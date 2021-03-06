import logging
from collections import namedtuple
from operator import attrgetter

from django.conf import settings

from share import exceptions
from share import models
from share.util import IDObfuscator, InvalidID
from share.util.nameparser import HumanName


from .base import MatchingStrategy

logger = logging.getLogger(__name__)


class DatabaseStrategy(MatchingStrategy):
    MAX_NAME_LENGTH = 200

    def __init__(self, source=None, **kwargs):
        super().__init__(**kwargs)
        self.source = source

    def initial_pass(self, nodes):
        for node in nodes:
            if str(node.id).startswith('_:'):
                continue
            try:
                match = IDObfuscator.resolve(node.id)
                self.add_match(node, match)
            except InvalidID:
                pass

    def match_by_attrs(self, nodes, model, attr_names, allowed_models):
        self._match_query(
            nodes,
            model,
            column_names=[model._meta.get_field(a).column for a in attr_names],
            get_values=lambda node: [node[a] for a in attr_names],
            allowed_models=allowed_models,
        )

    def match_by_many_to_one(self, nodes, model, relation_names, allowed_models):
        node_values = {}
        for node in nodes:
            related_matches = {r: list(self.get_matches(node[r])) for r in relation_names}

            if all(len(matches) == 1 for matches in related_matches.values()):
                node_values[node] = [related_matches[r][0].id for r in relation_names]
            else:
                for relation_name, matches in related_matches.items():
                    if len(matches) > 1:
                        raise exceptions.MergeRequired(
                            'Multiple matches for node {}'.format(node[relation_name].id),
                            matches,
                        )

        self._match_query(
            node_values.keys(),
            model,
            column_names=[model._meta.get_field(r).column for r in relation_names],
            get_values=lambda node: node_values[node],
            allowed_models=allowed_models,
        )

    def match_by_one_to_many(self, nodes, model, relation_name):
        remote_fk_attr = model._meta.get_field(relation_name).remote_field.attname

        for node in nodes:
            match_ids = set(
                getattr(instance, remote_fk_attr)
                for related_node in node[relation_name]
                for instance in self.get_matches(related_node)
            )
            if match_ids:
                self.add_matches(node, model._meta.concrete_model.objects.filter(id__in=match_ids))

    def match_subjects(self, nodes):
        # Look for (taxonomy AND uri) first, then (taxonomy AND name)
        for node in nodes:
            if node['central_synonym'] is None:
                # Central taxonomy
                qs = models.Subject.objects.filter(central_synonym__isnull=True)
            elif self.source:
                # Custom taxonomy
                qs = models.Subject.objects.filter(taxonomy__source=self.source)
            else:
                continue

            for field_name in ('uri', 'name'):
                value = node[field_name]
                if value:
                    try:
                        match = qs.get(**{field_name: value})
                    except models.Subject.DoesNotExist:
                        continue
                    self.add_match(node, match)
                    break

    def match_agent_work_relations(self, nodes):
        work_nodes = set(n['creative_work'] for n in nodes)
        for work_node in work_nodes:
            for work in self.get_matches(work_node):
                agent_relations = models.AbstractAgentWorkRelation.objects.filter(
                    creative_work=work,
                )

                # Skip parsing all the names on Frankenwork's monster
                # TODO: work on defrankenization
                if agent_relations.count() > settings.SHARE_LIMITS['MAX_AGENT_RELATIONS']:
                    continue

                relation_nodes = [
                    n for n in work_node['agent_relations']
                    if not self.has_matches(n)
                ]
                if not relation_nodes:
                    continue

                relation_names = [
                    ParsedRelationNames(r, HumanName(r.cited_as), HumanName(r.agent.name))
                    for r in agent_relations.select_related('agent')
                    if len(r.cited_as) <= self.MAX_NAME_LENGTH and len(r.agent.name) <= self.MAX_NAME_LENGTH
                ]

                for node in relation_nodes:
                    if len(node['cited_as']) > self.MAX_NAME_LENGTH or len(node['agent']['name']) > self.MAX_NAME_LENGTH:
                        continue

                    node_names = ParsedRelationNames(node, HumanName(node['cited_as']), HumanName(node['agent']['name']))

                    top_matches = sorted(
                        filter(
                            attrgetter('valid_match'),
                            (ComparableAgentWorkRelation(node_names, r) for r in relation_names),
                        ),
                        key=attrgetter('sort_key'),
                        reverse=True,
                    )
                    if top_matches:
                        match = top_matches[0]
                        self.add_match(node['agent'], match.relation.agent)
                        self.add_match(node, match.relation)

    def _match_query(self, nodes, model, column_names, get_values, allowed_models):
        if not nodes:
            return

        if allowed_models:
            query_builder = ConstrainedTypeQueryBuilder(model._meta.db_table, column_names, get_values, allowed_models)
        else:
            query_builder = QueryBuilder(model._meta.db_table, column_names, get_values)

        node_map = {n.id: n for n in nodes}
        sql, values = query_builder.build(nodes)
        matches = model.objects.raw(sql, values)
        for match in matches:
            self.add_match(node_map[match.node_id], match)


ParsedRelationNames = namedtuple('ParsedRelationNames', ['obj', 'cited_as', 'agent_name'])


class ComparableAgentWorkRelation:
    def __init__(self, node_names, instance_names):
        self.relation = instance_names.obj
        self.node = node_names.obj

        # bit vector used to sort names by how close they are to the target name
        self._name_key = self._get_name_key(
            instance_names.cited_as,
            node_names.cited_as,
        )

        self.sort_key = (
            *self._name_key,
            *self._get_name_key(
                instance_names.agent_name,
                node_names.agent_name,
            ),
            self.relation.order_cited == self.node['order_cited'],
            self.relation._meta.model_name == self.node.type,
        )

    @property
    def valid_match(self):
        return any(c for c in self._name_key)

    def _get_name_key(self, parsed_name, parsed_target):
        # initial or None
        def i(name_part):
            return name_part[0] if name_part else None

        return (
            parsed_name.full_name == parsed_target.full_name,
            (parsed_name.first, parsed_name.last) == (parsed_target.first, parsed_target.last),
            (i(parsed_name.first), parsed_name.last) == (i(parsed_target.first), parsed_target.last),
        )


class QueryBuilder:
    QUERY_TEMPLATE = '''
        WITH nodes(node_id, {column_names}) AS (
            VALUES {value_placeholders}
        )
        SELECT nodes.node_id, {table_name}.*
        FROM nodes
        INNER JOIN {table_name} ON ({join_conditions})
    '''

    def __init__(self, table_name, column_names, get_values):
        self.table_name = table_name
        self.column_names = column_names
        self.get_values = get_values

    def build(self, nodes):
        sql = self.QUERY_TEMPLATE.format(
            table_name=self.table_name,
            column_names=', '.join(self.column_names),
            join_conditions=' AND '.join(self.join_conditions()),
            value_placeholders=', '.join('%s' for _ in nodes),
        )
        return (sql, self.params(nodes))

    def params(self, nodes):
        return [
            (n.id, *self.get_values(n))
            for n in nodes
        ]

    def join_conditions(self):
        return [
            'nodes.{column} = {table}.{column}'.format(
                column=column_name,
                table=self.table_name,
            )
            for column_name in self.column_names
        ]


class ConstrainedTypeQueryBuilder(QueryBuilder):
    TYPE_COLUMN = 'type'

    def __init__(self, table_name, column_names, get_values, allowed_models):
        super().__init__(table_name, column_names, get_values)
        self.allowed_models = allowed_models

    def params(self, nodes):
        return [
            *super().params(nodes),
            tuple(
                '{}.{}'.format(m._meta.app_label, m._meta.model_name)
                for m in self.allowed_models
            ),
        ]

    def join_conditions(self):
        return [
            *super().join_conditions(),

            '{table}.{column} IN %s'.format(
                column=self.TYPE_COLUMN,
                table=self.table_name,
            )
        ]

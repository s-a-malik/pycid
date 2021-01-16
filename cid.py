#Licensed to the Apache Software Foundation (ASF) under one or more contributor license
#agreements; and to You under the Apache License, Version 2.0.

import numpy as np
from pgmpy.models import BayesianModel
from pgmpy.factors.discrete import TabularCPD, DiscreteFactor
from pgmpy.factors.continuous import ContinuousFactor
import logging
from typing import List, Tuple
import itertools
from pgmpy.inference.ExactInference import BeliefPropagation
import networkx as nx
from cpd import NullCPD, FunctionCPD
import warnings
from pgmpy.models import MarkovModel


class CID(BayesianModel):
    def __init__(self, ebunch:List[Tuple[str, str]],
                 decision_nodes: List[str],
                 utility_nodes:List[str]):
        super(CID, self).__init__(ebunch=ebunch)
        self.decision_nodes = decision_nodes
        self.utility_nodes = utility_nodes

    def add_cpds(self, *cpds: DiscreteFactor) -> None:
        """Add the given CPDs and initiate NullCPDs and FunctionCPDs"""
        for cpd in cpds:
            if not isinstance(cpd, (TabularCPD, ContinuousFactor, NullCPD, FunctionCPD)):
                raise ValueError("Only TabularCPD, ContinuousFactor, FunctionCPD, or NullCPD can be added.")

            if set(cpd.scope()) - set(cpd.scope()).intersection(set(self.nodes())):
                raise ValueError("CPD defined on variable not in the model", cpd)

            for prev_cpd_index in range(len(self.cpds)):
                if self.cpds[prev_cpd_index].variable == cpd.variable:
                    logging.warning(
                        "Replacing existing CPD for {var}".format(var=cpd.variable)
                    )
                    self.cpds[prev_cpd_index] = cpd
                    break
            else:
                self.cpds.append(cpd)

        # Once all CPDs are added, initialize the TablularCPD matrices
        for cpd in self.get_cpds():
            if hasattr(cpd, "initializeTabularCPD"):    #isinstance(cpd, FunctionCPD) or isinstance(cpd, NullCPD):
                cpd.initializeTabularCPD(self)

    def _get_valid_order(self, nodes:List[str]):
        srt = [i for i in nx.topological_sort(self) if i in nodes]
        return srt

    def check_sufficient_recall(self) -> bool:
        decision_ordering = self._get_valid_order(self.decision_nodes)
        for i, decision1 in enumerate(decision_ordering):
            for j, decision2 in enumerate(decision_ordering[i+1:]):
                for utility in self.utility_nodes:
                    if decision2 in self._get_ancestors_of(utility):
                        cid_with_policy = self.copy()
                        cid_with_policy.add_edge('pi',decision1)
                        observed = cid_with_policy.get_parents(decision2) + [decision2]
                        connected = cid_with_policy.is_active_trail('pi', utility, observed=observed)
                        #print(decision1, decision2, connected)
                        if connected:
                            logging.warning(
                                    "{} has insufficient recall of {} due to utility {}".format(
                                        decision2, decision1, utility)
                                    )
                            return False
        return True

    def impute_optimal_policy(self) -> None:
        """Impute a subgame perfect optimal policy to all decision nodes"""
        decisions = self._get_valid_order(self.decision_nodes)
        # solve in reverse ordering
        self.add_cpds(*[self._get_sp_policy(d) for d in reversed(decisions)])

    def impute_random_decision(self, d: str) -> None:
        sn = self.get_cpds(d).state_names
        self.add_cpds(NullCPD(d, self.get_cardinality(d), state_names=sn))

    def impute_random_policy(self) -> None:
        """Impute a random policy to all decision nodes"""
        for d in self.decision_nodes:
            self.impute_random_decision(d)

    def impute_conditional_expectation_decision(self, d: str, y: str) -> None:
        """Imputes a policy for d = the expectation of y conditioning on d's parents"""
        parents = self.get_parents(d)
        parent_values = [self.get_cpds(p).state_names[p] for p in parents]
        parent_values_prod = list(itertools.product(*parent_values))
        contexts = [ {p: pv[i] for i, p in enumerate(parents)}
                     for pv in parent_values_prod]
        #function = {pv: self.expected_value(y, contexts[i]) for i, pv in enumerate(parent_values_prod)}
        # func = lambda *x: function[x]
        new = self.copy()

        def cond_exp_policy(*pv: tuple) -> float:
            context = {p: pv[i] for i, p in enumerate(parents)}
            return new.expected_value(y, context)

        self.add_cpds(FunctionCPD(d, cond_exp_policy, parents))
        self.freeze_policy(d)

    def freeze_policy(self, d: str) -> None:
        """Replace a FunctionCPD with the corresponding TabularCPD, to prevent it from updating later"""
        self.add_cpds(self.get_cpds(d).convertToTabularCPD())

    def solve(self):
        """Return dictionary with subgame perfect global policy"""
        new_cid = self.copy()
        new_cid.impute_optimal_policy()
        return {d: new_cid.get_cpds(d) for d in new_cid.decision_nodes}

    def _possible_contexts(self, decision):
        parents = self.get_parents(decision)
        if parents:
            contexts = []
            parent_cards = [self.get_cardinality(p) for p in parents]
            context_tuples = itertools.product(*[range(card) for card in parent_cards])  # TODO this should use state names instead
            for context_tuple in context_tuples:
                contexts.append({p:c for p,c in zip(parents, context_tuple)})
            return contexts
        else:
            return None

    def _get_sp_policy(self, decision):
        actions = []
        contexts = self._possible_contexts(decision)
        if contexts:
            for context in contexts:
                act = self._optimal_decisions(decision, context)[0]
                actions.append(act)
        else:
            act = self._optimal_decisions(decision, {})[0]
            actions.append(act)

        def _indices_to_prob_table(indices, n_actions):
            return np.eye(n_actions)[indices].T

        prob_table = _indices_to_prob_table(actions, self.get_cardinality(decision))

        variable_card = self.get_cardinality(decision)
        evidence = self.get_parents(decision)
        evidence_card = [self.get_cardinality(e) for e in evidence]
        cpd = TabularCPD(
                decision,
                variable_card,
                prob_table,
                evidence,
                evidence_card,
                state_names=self.get_cpds(decision).state_names
                )
        return cpd

    def _optimal_decisions(self, decision, context):
        new = self.copy()
        new.impute_random_decision(decision)
        utilities = []
        #net = cid._impute_random_policy()
        acts = np.arange(self.get_cpds(decision).variable_card)
        for act in acts:
            context = context.copy()
            context[decision] = act
            ev = new.expected_utility(context)
            utilities.append(ev)
        indices = np.where(np.array(utilities)==np.max(utilities))
        if len(acts[indices])==0:
            warnings.warn('zero prob on {} so all actions deemed optimal'.format(context))
            return np.array(acts)
        return acts[indices]

    def _query(self, query, context):
        #outputs P(U|context)*P(context).
        #Use context={} to get P(U). Or use factor.normalize to get p(U|context)

        #query fails if graph includes nodes not in moralized graph, so we remove them
        # cid = self.copy()
        # mm = MarkovModel(cid.moralize().edges())
        # for node in self.nodes:
        #     if node not in mm.nodes:
        #         cid.remove_node(node)
        # filtered_context = {k:v for k,v in context.items() if k in mm.nodes}

        bp = BeliefPropagation(self)
        #factor = bp.query(query, filtered_context)
        factor = bp.query(query, context)
        return factor

    def expected_utility(self, context:dict):
        # for example:
        # cid = get_minimal_cid()
        # out = self.expected_utility({'D':1}) #TODO: give example that uses context
        factor = self._query(self.utility_nodes, context)
        factor.normalize() #make probs add to one

        ev = 0
        for idx, prob in np.ndenumerate(factor.values):
            utils = [factor.state_names[factor.variables[i]][j] for i,j in enumerate(idx) ]
            ev += np.sum(utils) * prob
        #ev = (factor.values * np.arange(factor.cardinality)).sum()
        return ev

    def expected_value(self, variable:str, context:dict) -> float:
        factor = self._query([variable], context)
        factor.normalize() #make probs add to one

        ev = 0.0
        for idx, prob in np.ndenumerate(factor.values):
            utils = [factor.state_names[factor.variables[i]][j] for i,j in enumerate(idx) ]
            ev += np.sum(utils) * prob
        #ev = (factor.values * np.arange(factor.cardinality)).sum()
        return ev

    # def check_model(self, allow_null=True):
    #     """
    #     Check the model for various errors. This method checks for the following
    #     errors.
    #     * Checks if the sum of the probabilities for each state is equal to 1 (tol=0.01).
    #     * Checks if the CPDs associated with nodes are consistent with their parents.
    #     Returns
    #     -------
    #     check: boolean
    #         True if all the checks are passed
    #     """
    #     for node in self.nodes():
    #         cpd = self.get_cpds(node=node)
    #
    #         if cpd is None:
    #             raise ValueError("No CPD associated with {}".format(node))
    #         elif isinstance(cpd, (NullCPD, FunctionCPD)):
    #             if not allow_null:
    #                 raise ValueError(
    #                     "CPD associated with {node} is null or function cpd".format(node=node)
    #                 )
    #         elif isinstance(cpd, (TabularCPD, ContinuousFactor)):
    #             evidence = cpd.get_evidence()
    #             parents = self.get_parents(node)
    #             if set(evidence if evidence else []) != set(parents if parents else []): #TODO: do es this check appropriate cardinalities?
    #                 raise ValueError(
    #                     "CPD associated with {node} doesn't have "
    #                     "proper parents associated with it.".format(node=node)
    #                 )
    #             if not cpd.is_valid_cpd():
    #                 raise ValueError(
    #                     "Sum or integral of conditional probabilites for node {node}"
    #                     " is not equal to 1.".format(node=node)
    #                 )
    #     return True

    def copy(self):
        model_copy = CID(self.edges(), decision_nodes=self.decision_nodes, utility_nodes=self.utility_nodes)
        if self.cpds:
            model_copy.add_cpds(*[cpd.copy() for cpd in self.cpds])
        return model_copy

    def __get_color(self, node):
        if node.startswith('D'):
            return 'lightblue'
        elif node.startswith('U'):
            return 'yellow'
        else:
            return 'lightgray'

    def draw(self):
        l = nx.kamada_kawai_layout(self)
        colors = [self.__get_color(node) for node in self.nodes]
        nx.draw_networkx(self, pos=l, node_color=colors)

"""The narrators of a case's outcome (#2615 stage S2; empty until then).

A narrator composes the text that states a case's outcome: the reply's
outcome lines, the offers' prose, the notices, the prompt's outcome blocks.
It renders the recorded outcome and does not derive one (Principle 13.2 and
13.3). The ``.importlinter`` contract "Narrators render the outcome, never
derive it" forbids this package to import ``cause_assurance``,
``causal_graph.disconfirmation`` or ``terminal_transitions``, directly or
through a chain.
"""

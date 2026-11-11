"""The narrators of a case's outcome (#2615 stage S2, #2636).

A narrator composes the text that states a case's outcome: the reply's
outcome lines, the offers' prose, the notices, the prompt's outcome blocks.
It renders the recorded outcome and does not derive one (Principle 13.2 and
13.3). The composers here are keyed on what a decider decided, a reason and
its params (``offers``, ``notices``); the renderers of the decision log's
notice codes (``codes``, ``notices.render_notice``) also read the cycle's
record, after its last decider ran (#2636 B1b). The
``.importlinter`` contract "Narrators render the outcome, never derive it"
forbids imports of ``cause_assurance``, ``causal_graph.disconfirmation`` and
``terminal_transitions`` from this package, direct or through other modules,
as import-linter sees them. It does
not cover attribute access through a package object or ``importlib``. Every
directory here needs an ``__init__.py``, or the contract cannot see it.
"""

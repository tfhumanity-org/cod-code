"""harnesslab - an educational, readable reference implementation of a coding-agent harness.

Clean-room implementation of the functional specification in ``spec/`` (see
``spec/INDEX.md``). Standard library only; runs offline with
``harnesslab.model.scripted.ScriptedModel``. See README.md for the module map
and ARCHITECTURE.md for the data flow of one turn.

Subpackage ``__init__`` modules are intentionally empty of re-exports so that
importing a package never drags in modules that are still being implemented.
"""

__version__ = "0.1.0"

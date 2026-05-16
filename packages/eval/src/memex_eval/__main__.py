"""Entry point so ``python -m memex_eval`` invokes the Typer app.

The CLI is normally invoked via the ``memex-eval`` script registered as
a setuptools entry point. ``python -m memex_eval`` is the interpreter-
explicit alternative — important for subprocess invocations from within
the package (e.g. ``memex-eval suite refresh-snapshot``) so the child
and parent share the same interpreter + installed package, instead of
resolving an unrelated ``memex-eval`` binary from PATH.
"""

from memex_eval.cli import app

if __name__ == '__main__':
    app()

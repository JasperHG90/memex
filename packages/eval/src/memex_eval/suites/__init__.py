"""Built-in evaluation suites for memex.

Each subpackage is a Suite — exports a top-level ``SUITE: Suite``
constant. The loader walks this package via ``pkgutil.iter_modules``
to discover all available suites.
"""

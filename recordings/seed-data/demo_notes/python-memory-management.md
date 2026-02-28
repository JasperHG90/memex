# Python Memory Management

## Overview

Python uses a multi-layered memory management system that combines reference counting with a cyclic garbage collector. Understanding how Python manages memory is essential for writing efficient applications, particularly when dealing with large datasets or long-running processes.

## Reference Counting

At its core, Python tracks object lifetimes using reference counting. Every Python object maintains a count of how many references point to it. When an object's reference count drops to zero, the interpreter immediately deallocates it and reclaims the memory.

```python
import sys

x = [1, 2, 3]
print(sys.getrefcount(x))  # Shows current reference count

y = x  # Increments refcount
del y  # Decrements refcount
```

Reference counting is deterministic and has low overhead for most operations, but it cannot handle circular references where two or more objects reference each other.

## Cyclic Garbage Collector

Python's `gc` module implements a generational garbage collector specifically designed to detect and collect circular references. Objects are divided into three generations:

- **Generation 0**: Newly created objects. Collected most frequently.
- **Generation 1**: Objects that survived one collection cycle.
- **Generation 2**: Long-lived objects. Collected least frequently.

The generational approach is based on the observation that most objects are short-lived. By collecting younger generations more frequently, Python minimizes the overhead of garbage collection while still reclaiming memory from circular references.

```python
import gc

gc.get_threshold()  # Returns (700, 10, 10) by default
gc.collect()        # Force a full collection
```

## Memory Pools and Allocators

Python uses a custom memory allocator called `pymalloc` that is optimized for small objects (up to 512 bytes). This allocator manages memory in pools and arenas:

- **Arenas**: Large blocks of 256 KB obtained from the system allocator.
- **Pools**: Each arena is divided into 4 KB pools.
- **Blocks**: Each pool is subdivided into fixed-size blocks.

This pooling strategy reduces fragmentation and the overhead of frequent `malloc`/`free` calls. For objects larger than 512 bytes, Python falls back to the system's default memory allocator.

## Memory Optimization with __slots__

By default, Python objects store their attributes in a dictionary (`__dict__`), which adds significant memory overhead. Using `__slots__` replaces the dictionary with a fixed set of attribute slots, reducing memory consumption by 30-50% per instance.

```python
class Point:
    __slots__ = ('x', 'y', 'z')

    def __init__(self, x: float, y: float, z: float):
        self.x = x
        self.y = y
        self.z = z
```

This is particularly valuable in data-intensive applications where millions of small objects are created, such as nodes in a graph database, records in a machine learning pipeline, or entities in an in-memory data store.

## Weak References

The `weakref` module allows creating references to objects that do not increment the reference count. This is useful for implementing caches and observer patterns without preventing garbage collection.

```python
import weakref

class DataCache:
    def __init__(self):
        self._cache = weakref.WeakValueDictionary()
```

## Practical Considerations

When working with large-scale data processing in Python, consider these strategies:

1. **Use generators** instead of lists to process data lazily and reduce peak memory usage.
2. **Leverage NumPy arrays** for numerical data, which store values in contiguous C arrays rather than as individual Python objects.
3. **Profile with tracemalloc** to identify memory hotspots.
4. **Consider object pooling** for frequently allocated and deallocated objects.

Understanding Python's memory model is crucial when building systems like vector databases or embedding stores, where millions of floating-point vectors must be managed efficiently. Libraries like DuckDB and NumPy bypass Python's object model entirely for bulk data, storing values in native arrays for both memory efficiency and computational speed.

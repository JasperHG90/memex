import hashlib


def calculate_deep_hash(metadata: bytes, content: bytes, aux_files: dict[str, bytes]) -> str:
    """Calculate a idempotent hash as unique identifier.

    Args:
        metadata (bytes): Serialized metadata (e.g., JSON bytes).
        content (bytes): Main content bytes.
        aux_files: dict[str, bytes]): Auxiliary files as a dictionary of
          filename to bytes (e.g. images, attachments).

    Returns:
        str: Hexadecimal MD5 hash string.
    """
    hasher = hashlib.md5()

    hasher.update(metadata)

    hasher.update(content)

    # 3. Hash Aux Files (Filename + Content)
    if aux_files:
        for name in sorted(aux_files.keys()):
            hasher.update(name.encode('utf-8'))
            hasher.update(aux_files[name])

    return hasher.hexdigest()

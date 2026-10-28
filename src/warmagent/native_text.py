"""Optional C++ batch scoring; Python owns Unicode normalization and policy."""
import ctypes
from pathlib import Path
import sys


_filename = 'text_kernels.dll' if sys.platform == 'win32' else 'libtext_kernels.so'
_path = Path(__file__).with_name('_native') / _filename
_library = ctypes.CDLL(str(_path)) if _path.is_file() else None
if _library is not None:
    _library.warm_overlap_batch.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_double)]
    _library.warm_overlap_batch.restype = ctypes.c_int


def overlap_python(query, texts):
    """Reference implementation on already normalized text; one query set."""
    left = {query[i:i + 2] for i in range(max(0, len(query) - 1))}
    scores = []
    for text in texts:
        right = {text[i:i + 2] for i in range(max(0, len(text) - 1))}
        scores.append(len(left & right) / max(1, min(len(left), len(right))))
    return scores


def overlap_batch(query, texts):
    if not texts:
        return []
    if _library is None:
        return overlap_python(query, texts)
    return _native_scores(query, texts, _library.warm_overlap_batch)


def _native_scores(query, texts, function):
    # Native-endian UTF-32 preserves Python codepoints, including lone
    # surrogates. Normalization remains exactly Python's lower/regex rules.
    codec = 'utf-32-le' if sys.byteorder == 'little' else 'utf-32-be'
    query_bytes = query.encode(codec, errors='surrogatepass')
    encoded = [text.encode(codec, errors='surrogatepass') for text in texts]
    offsets = [0]
    for data in encoded:
        offsets.append(offsets[-1] + len(data) // 4)
    joined = b''.join(encoded)
    left = (ctypes.c_uint32 * (len(query_bytes) // 4)).from_buffer_copy(query_bytes)
    right = (ctypes.c_uint32 * (len(joined) // 4)).from_buffer_copy(joined)
    bounds = (ctypes.c_size_t * len(offsets))(*offsets)
    scores = (ctypes.c_double * len(texts))()
    if function(left, len(left), right, bounds, len(texts), scores):
        raise RuntimeError('native_overlap_failed')
    return list(scores)


def backend():
    return 'c++' if _library is not None else 'python'

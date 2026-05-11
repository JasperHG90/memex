"""Re-export of memex_common.redaction for convenient suite-side imports.

The walker itself lives in ``memex_common.redaction`` so the FastAPI
server can apply it to ``GET /api/v1/system/config`` payloads. This
module is a thin re-export so eval code can import it from the suite
namespace.
"""

from memex_common.redaction import REDACTED, redact

__all__ = ['redact', 'REDACTED']

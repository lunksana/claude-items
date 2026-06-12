"""DNS 上游 resolver 适配（UDP / DoT / DoH）。"""

from .upstream import make_upstream, UdpUpstream, DotUpstream, DohUpstream  # noqa: F401

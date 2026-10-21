"""Optional social collector (X/Twitter, Reddit) used only when the
official API and its terms of use allow it (section 20). Must be
disable-able independently of the rest of the news radar.

Roadmap: PHASE 5 (optional / behind a feature flag). Not implemented yet — Phase 1 only wires the
architecture so imports, tests and config loading all work end to end.
"""

class SocialCollector:
    """See module docstring."""

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError("SocialCollector is scaffolded in Phase 1; implemented in a later phase per ROADMAP.md")

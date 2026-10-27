class RatchetError(Exception):
    """A failure the user can fix (bad input, bad config, corrupt ledger). The CLI prints it without a traceback."""

    exit_code = 1


class RoundCapReached(RatchetError):
    """`ratchet review` refused to start another review round of a change that used up max_review_rounds."""

    exit_code = 3


class RoundKeyUnavailable(RatchetError):
    """No name for the change under review: detached HEAD, no default branch to measure from, or no shared history with it."""

class RatchetError(Exception):
    """A failure the user can fix (bad input, bad config, corrupt ledger). The CLI prints it without a traceback."""

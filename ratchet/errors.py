class RatchetError(Exception):
    """A failure the user can fix (bad input, bad config, corrupt ledger). The CLI prints it without a traceback."""

    exit_code = 1


class RoundCapReached(RatchetError):
    """`ratchet review` refused to start another review round of a change that used up max_review_rounds."""

    exit_code = 3


class RoundKeyUnavailable(RatchetError):
    """No name for the change under review: detached HEAD, no default branch to measure from, or no shared history with it."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason  # a few words naming the cause, for the one line `count` prints


class SharedLayerMissing(RatchetError):
    """A rules layer outside the repo, shared with other repos, is not on this machine. Without its clauses a rule set is
    incomplete, so a command that records or writes against it refuses; one that only compares may go on without the layer."""

    def __init__(self, name: str, location: str) -> None:
        self.name = name  # as the config names it
        self.what = f"rules layer {name} is not on this machine (looked for {location})"
        super().__init__(f"{self.what}. It lies outside this repository, and without its clauses this command would run on an "
                         "incomplete rule set: put the file there, or run this on a machine that has it")

# Fixture rules

## Exceptions

1. **[P0E.1]** Keep the guarded block to the one line expected to fail. (blocker)

## Reliability

1. **[REL.1]** When replacing a usable artifact, keep its only usable copy until the replacement is ready. (should-fix) <!-- digest: Keep usable state until the replacement is ready -->
   Scope: installed files and configuration. A disposable temporary file is exempt.
   Violates: delete the old file, then write the new one.
   Complies: write beside it, validate, then rename over it.

2. **[REL.2]** A change to how persistent ids are made needs a migration or compatible reads. (should-fix)
   A display-only change is exempt.

3. **[REL.3]** A rule whose block carries a table, so that taking the block out would take the table too. (should-fix)

   | family | severity |
   |---|---|
   | P0E | blocker |

## Reviewer

1. **[R.1]** Standards come from this file, not from the reviewer.

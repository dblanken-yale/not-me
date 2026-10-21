---
schema: 1                        # config format version
target: {strategy: [deployments, bot-comment, url-template, local], url_template: null, bot_comment_regex: null}  # how to find the URL to test, tried in order
steps_heading: "(Functional )?[Tt]esting steps"  # regex for the PR heading that holds testing steps
fail_label: needs_work           # labels key applied when proof fails
give_up_after: 3                 # attempts before asking for a person
screenshots: none                # none | failures | all
---
# Prove

## Logging in to the target

Describe how to log in to the test environment. Never put secrets here.

## How to run steps

Follow the PR's testing steps in order, in a browser, and record each result.

## What a pass looks like

Every step behaves as written, with no errors in the page or console.

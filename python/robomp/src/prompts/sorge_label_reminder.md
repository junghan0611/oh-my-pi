Turn ended without a verdict.

Issue: {{repo.full_name}}#{{issue.number}} — {{issue.title}}

You were woken to judge this issue's labels and no label call landed. Exactly
one turn-ending action:

1. `set_issue_labels` — the verdict, one value per single-value axis
   (`state:` `ball:` `priority:` `brief:`), any number of `house:`. Values
   unchanged from the current set are still a verdict; re-assert them.
2. `abort_task` — the evidence genuinely does not support any label; say
   exactly what is missing.

Do not comment, close, edit files, or explain instead of acting: prose reaches
nobody. MUST end this turn by calling one of the two tools.

1) VERDICT: your decision and the most important reason, at most 80 words.
2) Findings, or "None found". For each: severity, file/symbol evidence,
   premises, failure mechanism, likely impact, smallest repair, regression
   that fails before the repair, confidence, and missing context.
3) What you would delete, and why that is safe.
4) Alternatives, if useful.
5) One-line recommendation.
6) PROOF: copy the proof target's last non-empty line exactly.

Print the review as your final assistant message after all tools.
The next line is metadata; do not print it in your answer.
RESULT_REQUIRE: VERDICT

End with two separate plain lines, substituting the actual token:
6) PROOF: MANIFEST_TAIL_TOKEN: <actual token>
RESULT_COMPLETE

If you cannot review the packet, end with RESULT_INVALID instead.

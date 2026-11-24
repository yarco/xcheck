1) VERDICT: your decision and the most important reason, at most 80 words.
2) Findings, or "None found". For each, give severity, evidence (file, symbol,
   behavior), premises, failure mechanism, likelihood and impact. Give the
   smallest repair (code or pseudodiff if supported), what it must preserve,
   how to check the finding and repair, and any uncertainty or missing context.
3) Maintainability for code and design reviews. Use the packet's scenario or
   pick one plausible follow-up or recurring operation from the staged evidence.
   Trace the same scenario through the candidate and an alternative that meets
   the same requirements. Use the pre-change design if it meets them. For each,
   name the edits or steps, the files or owners involved, and what must stay in
   sync. If the evidence supports no scenario or viable comparison, state the
   limit briefly and review any duplication or drift risk you can show.
   Put concrete findings in section 2. For any proposed deletion or reuse, name
   what changes, what still carries the intent or behavior, and how to check it.
   For a comment that restates the code, point to where the code or docs already
   explain it. For a redundant test or one that pins an implementation detail,
   identify the existing or simpler check that catches the same failure.
   "None found" is valid.
4) Alternatives, if useful.
5) One-line recommendation.

Print the review as your final assistant message after all tools.
The next line is packet metadata. Leave it out of your answer.
RESULT_REQUIRE: VERDICT

End with two separate plain lines. Use the proof target's exact last
non-empty line as the value of PROOF.
6) PROOF: MANIFEST_TAIL_TOKEN: <actual token>
RESULT_COMPLETE

If you cannot review the packet, use RESULT_INVALID as the final line instead.

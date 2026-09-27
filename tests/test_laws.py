"""Property laws for the pure parts of X-Check: the answer contract
(``xcheck.validate.check``) and the Grok stream reader.

Answers are built from a grammar of real answer shapes, not ``st.text()``:
arbitrary text is almost always "invalid" for trivial reasons, so a law stated
over it could not tell a working validator from ``return "invalid"``. Every
law below is checked from both sides: well-formed answers must pass, and each
single defect must fail naming that defect. A validator that always says
``done`` and one that always says ``invalid`` both fail this file.

Hypothesis is the only test-only dependency (``requirements-test.txt``) and is
imported unguarded: a skipped property lane would report OK for a proof that
never ran. Its storage directory is moved out of this cloud-synced folder
before the import.
"""

import os
import pathlib
import sys
import tempfile
import unittest

os.environ["HYPOTHESIS_STORAGE_DIRECTORY"] = str(pathlib.Path(tempfile.gettempdir()) / "xcheck-hypothesis")

from hypothesis import given, settings, strategies as st  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from xcheck import adapters, validate  # noqa: E402

SETTINGS = settings(database=None, deadline=None, max_examples=200)

KEYWORDS = st.sampled_from(["VERDICT", "Verdict", "DECISION"])
TOKENS = st.from_regex(r"MANIFEST_TAIL_TOKEN: [0-9a-f]{8,24}", fullmatch=True)
WORD = st.from_regex(r"[a-z]{3,10}", fullmatch=True)
SENTENCE = st.lists(WORD, min_size=4, max_size=12).map(lambda w: " ".join(w).capitalize() + ".")
HEADINGS = st.sampled_from([
    "### 1) {k}", "1) {k}", "**1) {k}**", "## {k} ##", "{k}:", "1) {k} (≤80 words)", "# {k}: HOLD",
    "- **1) {k}**", "* 1) {k}:",
])


@st.composite
def answer(draw):
    """(parts, contract): a complete answer as named parts, in order."""
    keyword, token = draw(KEYWORDS), draw(TOKENS)
    heading = draw(HEADINGS).format(k=keyword)
    body = "\n".join(draw(st.lists(SENTENCE, min_size=1, max_size=4)))
    proof = draw(st.sampled_from([
        f"6) PROOF: {token}", f"6) PROOF\n{token}", f"6) PROOF: `{token}`", f"**6) PROOF:** {token}",
        f"- 6) PROOF: {token}", f"* **6) PROOF:** {token}", f"* 6) PROOF: {token}",
    ]))
    parts = {"heading": heading, "body": body, "proof": proof, "marker": validate.COMPLETE}
    return parts, validate.Contract(keyword, "evidence/manifest-tail.txt", token)


def render(parts: dict) -> str:
    return "\n".join(v for v in parts.values() if v is not None) + "\n"


class AnswerContractLaws(unittest.TestCase):
    @given(answer())
    @SETTINGS
    def test_well_formed_answers_are_done(self, a):
        """Property: every grammar-built complete answer is done with no problems.
        Oracle: the construction. Catches: a validator that rejects a legal
        heading shape or either legal PROOF form."""
        parts, contract = a
        self.assertEqual(validate.check(render(parts), contract), ("done", []))

    @given(answer(), st.sampled_from(["heading", "body", "proof", "marker"]))
    @SETTINGS
    def test_each_missing_part_is_named_and_alone(self, a, missing):
        """Property: removing exactly one required part makes the answer invalid,
        and the only problem reported is that part. Oracle: which part was
        removed. Catches: an always-done validator, and checks that blame the
        wrong part (a missing body reported as a missing heading)."""
        parts, contract = a
        parts[missing] = None
        status, problems = validate.check(render(parts), contract)
        expected = {
            "heading": f"missing required heading: {contract.require}",
            "body": "no review text besides the heading, PROOF and marker",
            "proof": "missing PROOF line",
            "marker": f"last line is not {validate.COMPLETE}",
        }[missing]
        self.assertEqual((status, problems), ("invalid", [expected]))

    @given(answer(), st.data())
    @SETTINGS
    def test_any_changed_token_character_fails_the_proof(self, a, data):
        """Property: altering one character of the proof token is caught.
        Oracle: the contract token. Catches: substring or prefix matching."""
        parts, contract = a
        token = contract.token
        i = data.draw(st.integers(len("MANIFEST_TAIL_TOKEN: "), len(token) - 1))
        swapped = token[:i] + ("0" if token[i] != "0" else "1") + token[i + 1:]
        parts["proof"] = parts["proof"].replace(token, swapped)
        self.assertEqual(validate.check(render(parts), contract),
                         ("invalid", ["PROOF does not match the proof target's last line"]))

    @given(answer(), st.integers(0, 3), st.booleans())
    @SETTINGS
    def test_trailing_blank_lines_and_crlf_do_not_change_the_verdict(self, a, blanks, crlf):
        """Property: line endings and trailing blank lines are not signal.
        Oracle: the verdict of the plain rendering (done, by the first law)."""
        parts, contract = a
        text = render(parts) + "\n" * blanks
        if crlf:
            text = text.replace("\n", "\r\n")
        self.assertEqual(validate.check(text, contract), ("done", []))

    @given(answer())
    @SETTINGS
    def test_a_final_result_invalid_is_always_invalid(self, a):
        """Property: a peer that ends with RESULT_INVALID declined the review,
        whatever precedes it. Catches: letting a complete-looking body override
        the peer's own declaration."""
        parts, contract = a
        parts["marker"] = validate.INVALID
        self.assertEqual(validate.check(render(parts), contract)[0], "invalid")

    @given(answer(), SENTENCE)
    @SETTINGS
    def test_text_after_the_marker_is_not_complete(self, a, extra):
        """Property: RESULT_COMPLETE must be the last non-empty line."""
        parts, contract = a
        status, problems = validate.check(render(parts) + extra + "\n", contract)
        self.assertEqual(status, "invalid")
        self.assertIn(f"last line is not {validate.COMPLETE}", problems)


def feed_grok(events) -> str:
    r = adapters.Reader("grok")
    for e in events:
        r.feed(e)
    r.finish(pathlib.Path("/nonexistent"))
    return r.text


@st.composite
def chunks(draw, text: str):
    cuts = sorted(draw(st.sets(st.integers(1, max(1, len(text) - 1)), max_size=8)))
    bounds = [0, *[c for c in cuts if c < len(text)], len(text)]
    return [text[a:b] for a, b in zip(bounds, bounds[1:]) if b > a]


class GrokReaderLaws(unittest.TestCase):
    @given(answer(), st.data())
    @SETTINGS
    def test_chunking_does_not_change_the_answer(self, a, data):
        """Property: however the final message is split into text events, the
        answer is that message. Oracle: the unsplit string."""
        text = render(a[0])
        events = [{"type": "text", "data": c} for c in data.draw(chunks(text))]
        self.assertEqual(feed_grok(events + [{"type": "usage"}, {"type": "end"}]), text)

    @given(answer(), st.lists(st.tuples(SENTENCE, st.booleans()), max_size=4), st.data())
    @SETTINGS
    def test_earlier_messages_never_leak_into_the_answer(self, a, earlier, data):
        """Property: narration in earlier messages (each closed by usage, some
        followed by a tool call) is never part of the answer. Oracle: the
        construction. Catches: gluing narration onto the answer's heading."""
        text = render(a[0])
        events = []
        for narration, tool in earlier:
            events += [{"type": "text", "data": narration}, {"type": "usage"}]
            if tool:
                events.append({"type": "tool_call", "toolName": "read_file", "rawInput": {}})
        events += [{"type": "text", "data": c} for c in data.draw(chunks(text))]
        events += [{"type": "usage"}, {"type": "end"}]
        self.assertEqual(feed_grok(events), text)

    @given(st.lists(SENTENCE, min_size=1, max_size=4))
    @SETTINGS
    def test_a_stream_ending_in_a_tool_call_has_no_answer(self, narration):
        """Property: if the last event of substance is a tool call, there is no
        answer; narration before it must not be promoted to one."""
        events = []
        for n in narration:
            events += [{"type": "text", "data": n}, {"type": "usage"}]
        events += [{"type": "tool_call", "toolName": "read_file", "rawInput": {}}, {"type": "end"}]
        self.assertEqual(feed_grok(events), "")


if __name__ == "__main__":
    unittest.main()

import unittest

from server import CCPeekHandler


class SearchTokenScoringTests(unittest.TestCase):
    def test_all_terms_fallback_scores_token_presence_not_repetition(self):
        handler = object.__new__(CCPeekHandler)
        patterns = CCPeekHandler._build_token_patterns("main line of the pr")

        result = handler._token_search(
            "main main main line of the pr pr pr",
            patterns,
        )

        self.assertEqual(result, (5, 5))

    def test_plain_word_match_requires_boundaries(self):
        self.assertTrue(CCPeekHandler._has_plain_word_match("main pr line", "pr"))
        self.assertFalse(CCPeekHandler._has_plain_word_match("main prompt line", "pr"))

    def test_search_request_sequence_marks_older_requests_stale(self):
        CCPeekHandler._note_search_request("client-a", 2)

        self.assertTrue(CCPeekHandler._is_search_request_stale("client-a", 1))
        self.assertFalse(CCPeekHandler._is_search_request_stale("client-a", 2))
        self.assertFalse(CCPeekHandler._is_search_request_stale("client-a", 3))

"""
Regression test: the extractor's get_info()["annotationGuides"] must validate
against the shared AnnotationGuideInfo/ExtractorInfo Pydantic models. A schema
drift here previously caused HTTP 500 on the entire /api/v1/extract/list
endpoint, because extraction.py's router has no per-extractor error isolation.

@testCovers plugins/grobid/src/pte_plugin_grobid/extractor.py
"""

import unittest

from fastapi_app.lib.models.models_extraction import ExtractorInfo


class TestExtractorInfoValidation(unittest.TestCase):
    def test_grobid_extractor_info_validates(self):
        from pte_plugin_grobid.extractor import GrobidTrainingExtractor
        info = GrobidTrainingExtractor.get_info()
        ExtractorInfo(**{**info, "available": True})

    def test_review_units_are_variants(self):
        from pte_plugin_grobid.extractor import GrobidTrainingExtractor
        info = GrobidTrainingExtractor.get_info()
        self.assertEqual(info["reviewUnits"]["grobid.training.references"], ".//listBibl/bibl")
        self.assertTrue(set(info["reviewUnits"]) <= set(info["variants"]))


if __name__ == "__main__":
    unittest.main()

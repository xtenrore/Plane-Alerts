from app.private_ops.provider_probe import run


class FakeAdapter:
    def probe(self, slot):
        if slot.name == "GROQ_KEY":
            raise RuntimeError("never print credential: SECRET_TEST_ONLY")
        return "available"


def test_metadata_probe_redacts_failures_and_omits_incomplete_pairs():
    result = run({"GROQ_KEY": "SECRET_TEST_ONLY", "GROQ_KEY_2": "another",
                  "CLOUDFLARE_API_TOKEN": "unpaired"}, FakeAdapter())
    assert result == {"GROQ_KEY": "unavailable", "GROQ_KEY_2": "available"}
    assert "SECRET_TEST_ONLY" not in repr(result)

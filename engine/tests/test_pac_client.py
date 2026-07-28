

# ---------------------------------------------------------------------------
# URLs printed in prose, not in a table
# ---------------------------------------------------------------------------

def test_a_url_at_the_end_of_a_sentence_loses_its_full_stop():
    """PAC prints URLs both in tables and in prose. `https?://\\S+` swallows the
    sentence's full stop, and the result is only ever shown when PAC is already
    reporting an error -- the worst moment to display a URL that is subtly not
    the one the operator configured.
    """
    from agent_liftoff.connectors.copilot_studio.pac_client import clean_url

    assert clean_url("https://contoso.crm8.dynamics.com/.") == "https://contoso.crm8.dynamics.com/"
    assert clean_url("https://contoso.crm8.dynamics.com/,") == "https://contoso.crm8.dynamics.com/"
    assert clean_url("https://contoso.crm8.dynamics.com/).") == "https://contoso.crm8.dynamics.com/"


def test_a_bare_url_is_left_exactly_as_it_is():
    from agent_liftoff.connectors.copilot_studio.pac_client import clean_url

    url = "https://contoso.crm8.dynamics.com/"
    assert clean_url(url) == url

from voice_bridge.conversation import ConversationStore, new_id

def test_conversation_store_caps_history():
    store = ConversationStore(max_turns=2)
    for i in range(6):
        store.add("c", "user", str(i))
    assert len(store.get("c")) == 4

def test_ids_have_prefix():
    assert new_id("resp").startswith("resp_")

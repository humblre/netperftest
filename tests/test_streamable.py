from netperftest.netperftest import CHUNK_SIZE, Streamable


def test_len_reports_total_length():
    assert len(Streamable(1000)) == 1000


def test_iter_yields_exact_total():
    stream = Streamable(CHUNK_SIZE * 2 + 5, chunk_size=CHUNK_SIZE)
    chunks = list(stream)
    assert sum(len(chunk) for chunk in chunks) == CHUNK_SIZE * 2 + 5
    assert all(chunk == bytes(len(chunk)) for chunk in chunks)


def test_iter_is_repeatable():
    # Regression: iterating twice must yield the same payload. The original
    # implementation mutated chunk_size mid-iteration and corrupted the second
    # pass, which broke transparent request retries.
    stream = Streamable(CHUNK_SIZE + 1)
    first = [len(chunk) for chunk in stream]
    second = [len(chunk) for chunk in stream]
    assert first == second
    assert sum(first) == CHUNK_SIZE + 1


def test_zero_length_yields_nothing():
    assert list(Streamable(0)) == []

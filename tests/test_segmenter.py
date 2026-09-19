"""Tests for the deterministic multi-item capture segmenter."""

from rysos.ai.segmenter import segment_capture_message


def test_single_item_passthrough():
    assert segment_capture_message("boleto da Luzsul pago hoje") == ["boleto da Luzsul pago hoje"]


def test_empty_input():
    assert segment_capture_message("   ") == []


def test_splits_on_semicolon():
    segments = segment_capture_message("proposta enviada; paguei a Nimbus Cloud; contratei o Wagner")
    assert segments == ["proposta enviada", "paguei a Nimbus Cloud", "contratei o Wagner"]


def test_no_semicolon_stays_one_note_even_with_multiple_sentences():
    # Sentence-boundary splitting only activates once ';' signals a multi-item report —
    # otherwise an ordinary two-sentence capture would fragment into confirmation spam.
    text = (
        "novo projeto de fusão com a Beta, prazo outubro. Stakeholder é o Carlos."
    )
    assert segment_capture_message(text) == [text]


def test_splits_sentence_boundary_within_a_semicolon_chunk():
    text = (
        "reuniao com Heitor feita; "
        "Agora preciso intensificar o go-to-market da Meridian para acelerar a geração "
        "de receita. Preciso verificar com o Jair na Helix sobre a questão contabil."
    )
    segments = segment_capture_message(text)
    assert len(segments) == 3
    assert segments[0] == "reuniao com Heitor feita"
    assert segments[1].startswith("Agora preciso intensificar")
    assert segments[2].startswith("Preciso verificar com o Jair")


def test_does_not_split_on_decimal_or_currency():
    text = "paguei a dívida no valor aproximado de R$ 16.000 no cartão de crédito"
    assert segment_capture_message(text) == [text]


def test_semicolon_chunks_never_merge_even_if_short():
    segments = segment_capture_message("confecção das minutas acordada; valor de R$ 5 mil")
    assert segments == ["confecção das minutas acordada", "valor de R$ 5 mil"]


def test_short_sentence_fragment_merges_into_previous_within_same_chunk():
    text = "reuniao remarcada. Ok."
    assert segment_capture_message(text) == ["reuniao remarcada. Ok."]


def test_full_owner_report_yields_distinct_items():
    text = (
        "a proposta comercial da rede Horizonte Sul para o projeto de novos negocios (M&A) "
        "foi enviada com aprovacao da Vanessa Salgado e Gabriela Guerra; "
        "fiz o pagamento da divida da Meridian junto a Nimbus Cloud no valor aproximado de R$ 16 mil "
        "no cartao de credito; "
        "a confeccao das minutas contratuais da Meridian foi acordado no valor de R$ 5 mil "
        "a ser pago para o Wagner Junior; "
        "no evento HIS falei com o Heitor Sakamoto e contei das possibilidades de atuacao "
        "como advisor; "
        "no HIS tambem encontrei o Vinicius Oliveira que hoje esta na Sistemed-Tasy. "
        "Agora preciso intensificar o go-to-market da Meridian para acelerar a geracao de "
        "receita. "
        "Preciso verificar com o Jair na Helix sobre a questao contabil e tributaria."
    )
    segments = segment_capture_message(text)
    # 5 semicolon-delimited facts + the 2 trailing sentences that share no ';' divider.
    assert len(segments) == 7

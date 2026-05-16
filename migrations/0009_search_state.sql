CREATE TABLE embedding_models (
    column_name     TEXT         PRIMARY KEY,
    backend         TEXT         NOT NULL,
    model_name      TEXT         NOT NULL,
    dimension       INT          NOT NULL,
    activated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    retired_at      TIMESTAMPTZ
);

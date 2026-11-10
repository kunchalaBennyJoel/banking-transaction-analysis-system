-- Additive channel metadata for existing transactions.
-- Existing rows remain NULL (displayed by the application as Not recorded).
-- Apply once to a verified target database; this does not rewrite transaction data.
ALTER TABLE transactions
    ADD COLUMN channel VARCHAR(10) NULL AFTER status,
    ADD CONSTRAINT ck_transactions_channel
        CHECK (channel IS NULL OR channel IN ('UPI', 'ATM', 'BRANCH'));

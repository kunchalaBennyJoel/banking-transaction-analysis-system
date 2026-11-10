-- Deterministically classify the existing 2,400 synthetic transactions.
-- Only transactions.channel is updated. The routine is created and dropped by
-- this script so validation and rollback can be handled atomically in MySQL.
-- Run with the MySQL command-line client, which supports DELIMITER.

DROP PROCEDURE IF EXISTS codex_backfill_transaction_channels_003;

DELIMITER //
CREATE PROCEDURE codex_backfill_transaction_channels_003()
main: BEGIN
    DECLARE v_transaction_count BIGINT DEFAULT 0;
    DECLARE v_null_channel_count BIGINT DEFAULT 0;
    DECLARE v_upi_count BIGINT DEFAULT 0;
    DECLARE v_atm_count BIGINT DEFAULT 0;
    DECLARE v_branch_count BIGINT DEFAULT 0;
    DECLARE v_updated_count BIGINT DEFAULT 0;
    DECLARE v_message VARCHAR(256) DEFAULT NULL;

    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        GET DIAGNOSTICS CONDITION 1 v_message = MESSAGE_TEXT;
        ROLLBACK;
        DROP TEMPORARY TABLE IF EXISTS tmp_transaction_channel_assignment;
        RESIGNAL SET MESSAGE_TEXT = v_message;
    END;

    DROP TEMPORARY TABLE IF EXISTS tmp_transaction_channel_assignment;
    SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;
    START TRANSACTION;

    SELECT COUNT(*),
           COALESCE(SUM(channel IS NULL), 0),
           COALESCE(SUM(CAST(channel AS BINARY) = CAST('UPI' AS BINARY)), 0),
           COALESCE(SUM(CAST(channel AS BINARY) = CAST('ATM' AS BINARY)), 0),
           COALESCE(SUM(CAST(channel AS BINARY) = CAST('BRANCH' AS BINARY)), 0)
      INTO v_transaction_count,
           v_null_channel_count,
           v_upi_count,
           v_atm_count,
           v_branch_count
      FROM transactions;

    -- A completed rerun is a successful no-op only when the exact distribution
    -- is present and there are no NULL or unsupported values.
    IF v_transaction_count = 2400
       AND v_null_channel_count = 0
       AND v_upi_count = 1440
       AND v_atm_count = 600
       AND v_branch_count = 360 THEN
        COMMIT;
        SET @transaction_channel_backfill_result =
            'Already complete: 1,440 UPI, 600 ATM, 360 BRANCH, 0 NULL; no rows changed.';
        LEAVE main;
    END IF;

    -- Refuse every unexpected or partially populated state before any update.
    IF v_transaction_count <> 2400 OR v_null_channel_count <> 2400 THEN
        SET v_message = CONCAT(
            'Unexpected state; expected exactly 2,400 rows with NULL channels; found ',
            v_transaction_count, ' total and ', v_null_channel_count, ' NULL.'
        );
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = v_message;
    END IF;

    CREATE TEMPORARY TABLE tmp_transaction_channel_assignment (
        transaction_id INT NOT NULL PRIMARY KEY,
        assigned_channel VARCHAR(10) NOT NULL
    ) ENGINE=InnoDB;

    INSERT INTO tmp_transaction_channel_assignment (transaction_id, assigned_channel)
    SELECT ranked.id,
           CASE
               WHEN ranked.sequence_number <= 1440 THEN 'UPI'
               WHEN ranked.sequence_number <= 2040 THEN 'ATM'
               ELSE 'BRANCH'
           END
      FROM (
          SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS sequence_number
            FROM transactions
           WHERE channel IS NULL
      ) AS ranked;

    UPDATE transactions AS target
    JOIN tmp_transaction_channel_assignment AS assignment
      ON assignment.transaction_id = target.id
       SET target.channel = assignment.assigned_channel
     WHERE target.channel IS NULL;
    SET v_updated_count = ROW_COUNT();

    IF v_updated_count <> 2400 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Expected to update exactly 2,400 NULL-channel rows.';
    END IF;

    SELECT COUNT(*),
           COALESCE(SUM(channel IS NULL), 0),
           COALESCE(SUM(CAST(channel AS BINARY) = CAST('UPI' AS BINARY)), 0),
           COALESCE(SUM(CAST(channel AS BINARY) = CAST('ATM' AS BINARY)), 0),
           COALESCE(SUM(CAST(channel AS BINARY) = CAST('BRANCH' AS BINARY)), 0)
      INTO v_transaction_count,
           v_null_channel_count,
           v_upi_count,
           v_atm_count,
           v_branch_count
      FROM transactions;

    IF v_transaction_count <> 2400
       OR v_null_channel_count <> 0
       OR v_upi_count <> 1440
       OR v_atm_count <> 600
       OR v_branch_count <> 360 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Final channel distribution verification failed.';
    END IF;

    COMMIT;
    SET @transaction_channel_backfill_result =
        'SUCCESS: updated 2,400 rows; 1,440 UPI, 600 ATM, 360 BRANCH, 0 NULL.';
END//
DELIMITER ;

SET @transaction_channel_backfill_result = NULL;
CALL codex_backfill_transaction_channels_003();
SELECT @transaction_channel_backfill_result AS backfill_result;
DROP PROCEDURE codex_backfill_transaction_channels_003;

-- Additive authentication migration for MySQL 8.0.16+ / 8.4.
-- Apply once to a verified target database before creating the first admin.
CREATE TABLE employees (
    id INT NOT NULL AUTO_INCREMENT,
    employee_id VARCHAR(64) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(10) NOT NULL DEFAULT 'employee',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    CONSTRAINT uq_employees_employee_id UNIQUE (employee_id),
    CONSTRAINT ck_employees_role CHECK (role IN ('admin', 'employee'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

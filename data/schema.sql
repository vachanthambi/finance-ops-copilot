-- ============================================================
-- AI Finance Ops Copilot — source system schemas
-- Three schemas deliberately modelled as separate systems with
-- inconsistent keys, types, and currency conventions.
-- ============================================================

DROP SCHEMA IF EXISTS crm CASCADE;
DROP SCHEMA IF EXISTS erp CASCADE;
DROP SCHEMA IF EXISTS billing CASCADE;

CREATE SCHEMA crm;
CREATE SCHEMA erp;
CREATE SCHEMA billing;


-- ============================================================
-- CRM  — front office system
-- Customer key : 'ACC-00123' (string)
-- Dates        : VARCHAR, three inconsistent formats
-- Amounts      : local currency
-- ============================================================

CREATE TABLE crm.accounts (
    account_id      VARCHAR(12)  PRIMARY KEY,
    account_name    VARCHAR(200) NOT NULL,
    region          VARCHAR(10)  NOT NULL,
    industry        VARCHAR(60),
    currency_code   CHAR(3)      NOT NULL,
    owner_rep       VARCHAR(120),
    created_date    VARCHAR(30)             -- intentionally text
);

CREATE TABLE crm.opportunities (
    opportunity_id    VARCHAR(14)  PRIMARY KEY,
    account_id        VARCHAR(12)  NOT NULL REFERENCES crm.accounts(account_id),
    product_line      VARCHAR(60)  NOT NULL,
    stage             VARCHAR(30)  NOT NULL,
    close_date        VARCHAR(30),            -- intentionally text, mixed formats
    quantity          INTEGER,
    unit_price_local  NUMERIC(14,2),
    amount_local      NUMERIC(16,2),
    currency_code     CHAR(3)      NOT NULL
);

CREATE INDEX idx_crm_opp_account ON crm.opportunities(account_id);
CREATE INDEX idx_crm_opp_stage   ON crm.opportunities(stage);


-- ============================================================
-- ERP  — system of record for the general ledger
-- Customer key : integer surrogate
-- Dates        : proper DATE
-- Amounts      : USD only
-- ============================================================

CREATE TABLE erp.cost_centers (
    cost_center_id    INTEGER      PRIMARY KEY,
    cost_center_code  VARCHAR(20)  NOT NULL UNIQUE,
    cost_center_name  VARCHAR(120) NOT NULL,
    region            VARCHAR(10)  NOT NULL,
    function          VARCHAR(60)  NOT NULL
);

CREATE TABLE erp.customers (
    customer_id       INTEGER      PRIMARY KEY,
    customer_name     VARCHAR(200) NOT NULL,
    region            VARCHAR(10)  NOT NULL,
    cost_center_id    INTEGER      REFERENCES erp.cost_centers(cost_center_id),
    active_flag       BOOLEAN      DEFAULT TRUE
);

CREATE TABLE erp.gl_entries (
    entry_id          BIGSERIAL    PRIMARY KEY,
    customer_id       INTEGER      NOT NULL REFERENCES erp.customers(customer_id),
    cost_center_id    INTEGER      NOT NULL REFERENCES erp.cost_centers(cost_center_id),
    account_code      VARCHAR(10)  NOT NULL,
    account_name      VARCHAR(80)  NOT NULL,
    posting_date      DATE         NOT NULL,
    period_month      DATE         NOT NULL,   -- first day of the accounting month
    product_line      VARCHAR(60),
    quantity          INTEGER,
    unit_price_usd    NUMERIC(14,4),
    amount_usd        NUMERIC(16,2) NOT NULL,
    source_doc        VARCHAR(30)
);

CREATE INDEX idx_erp_gl_period   ON erp.gl_entries(period_month);
CREATE INDEX idx_erp_gl_customer ON erp.gl_entries(customer_id);
CREATE INDEX idx_erp_gl_cc       ON erp.gl_entries(cost_center_id);

CREATE TABLE erp.budget (
    budget_id             BIGSERIAL    PRIMARY KEY,
    cost_center_id        INTEGER      NOT NULL REFERENCES erp.cost_centers(cost_center_id),
    product_line          VARCHAR(60)  NOT NULL,
    region                VARCHAR(10)  NOT NULL,
    period_month          DATE         NOT NULL,
    account_code          VARCHAR(10)  NOT NULL,
    budget_quantity       INTEGER      NOT NULL,
    budget_unit_price_usd NUMERIC(14,4) NOT NULL,
    budget_amount_usd     NUMERIC(16,2) NOT NULL
);

CREATE INDEX idx_erp_budget_period ON erp.budget(period_month);
CREATE INDEX idx_erp_budget_cc     ON erp.budget(cost_center_id);


-- ============================================================
-- BILLING  — invoicing platform
-- Customer key : email address
-- Dates        : TIMESTAMP
-- Amounts      : local currency, converted via monthly FX
-- ============================================================

CREATE TABLE billing.fx_rates (
    rate_month     DATE         NOT NULL,
    currency_code  CHAR(3)      NOT NULL,
    rate_to_usd    NUMERIC(14,6) NOT NULL,
    PRIMARY KEY (rate_month, currency_code)
);

CREATE TABLE billing.invoices (
    invoice_id          VARCHAR(20)  PRIMARY KEY,
    customer_email      VARCHAR(200) NOT NULL,
    customer_name_raw   VARCHAR(200),
    invoice_ts          TIMESTAMP    NOT NULL,
    currency_code       CHAR(3)      NOT NULL,
    fx_rate_applied     NUMERIC(14,6),
    total_amount_local  NUMERIC(16,2) NOT NULL,
    total_amount_usd    NUMERIC(16,2) NOT NULL,
    status              VARCHAR(20)  NOT NULL
);

CREATE INDEX idx_billing_inv_email ON billing.invoices(customer_email);
CREATE INDEX idx_billing_inv_ts    ON billing.invoices(invoice_ts);

CREATE TABLE billing.invoice_lines (
    line_id            BIGSERIAL     PRIMARY KEY,
    invoice_id         VARCHAR(20)   NOT NULL REFERENCES billing.invoices(invoice_id),
    product_line       VARCHAR(60)   NOT NULL,
    quantity           INTEGER       NOT NULL,
    unit_price_local   NUMERIC(14,2) NOT NULL,
    line_amount_local  NUMERIC(16,2) NOT NULL
);

CREATE INDEX idx_billing_lines_inv ON billing.invoice_lines(invoice_id);

--
-- PostgreSQL database dump
--

\restrict gTdRrRKwfMVZXr4kK1VOnkzvZYsBIggpWqMaRVWS6P9U8pue3RjOaBma4KPAt3r

-- Dumped from database version 16.13
-- Dumped by pg_dump version 16.13

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: gasbot
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.alembic_version OWNER TO gasbot;

--
-- Name: bank_transactions; Type: TABLE; Schema: public; Owner: gasbot
--

CREATE TABLE public.bank_transactions (
    id bigint NOT NULL,
    store_id character varying(64) NOT NULL,
    transaction_date date NOT NULL,
    amount numeric(10,2) NOT NULL,
    description character varying(256) NOT NULL,
    category character varying(64),
    transaction_type character varying(32),
    plaid_transaction_id character varying(128),
    matched_invoice_id integer,
    is_matched boolean DEFAULT false,
    last_updated_by character varying(16) DEFAULT 'bot'::character varying,
    last_updated_at timestamp without time zone DEFAULT now(),
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE public.bank_transactions OWNER TO gasbot;

--
-- Name: bank_transactions_id_seq; Type: SEQUENCE; Schema: public; Owner: gasbot
--

CREATE SEQUENCE public.bank_transactions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.bank_transactions_id_seq OWNER TO gasbot;

--
-- Name: bank_transactions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: gasbot
--

ALTER SEQUENCE public.bank_transactions_id_seq OWNED BY public.bank_transactions.id;


--
-- Name: conversation_history; Type: TABLE; Schema: public; Owner: gasbot
--

CREATE TABLE public.conversation_history (
    id bigint NOT NULL,
    store_id character varying(64) NOT NULL,
    role character varying(16) NOT NULL,
    content text NOT NULL,
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE public.conversation_history OWNER TO gasbot;

--
-- Name: conversation_history_id_seq; Type: SEQUENCE; Schema: public; Owner: gasbot
--

CREATE SEQUENCE public.conversation_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.conversation_history_id_seq OWNER TO gasbot;

--
-- Name: conversation_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: gasbot
--

ALTER SEQUENCE public.conversation_history_id_seq OWNED BY public.conversation_history.id;


--
-- Name: daily_sales; Type: TABLE; Schema: public; Owner: gasbot
--

CREATE TABLE public.daily_sales (
    id bigint NOT NULL,
    store_id character varying(64) NOT NULL,
    sale_date date NOT NULL,
    product_sales numeric(10,2) DEFAULT '0'::numeric,
    lotto_in numeric(10,2) DEFAULT '0'::numeric,
    lotto_online numeric(10,2) DEFAULT '0'::numeric,
    sales_tax numeric(10,2) DEFAULT '0'::numeric,
    gpi numeric(10,2) DEFAULT '0'::numeric,
    grand_total numeric(10,2) DEFAULT '0'::numeric,
    refunds numeric(10,2) DEFAULT '0'::numeric,
    lotto_po numeric(10,2),
    lotto_cr numeric(10,2),
    food_stamp numeric(10,2),
    cash_drop numeric(10,2) DEFAULT '0'::numeric,
    card numeric(10,2) DEFAULT '0'::numeric,
    check_amount numeric(10,2) DEFAULT '0'::numeric,
    atm numeric(10,2) DEFAULT '0'::numeric,
    pull_tab numeric(10,2) DEFAULT '0'::numeric,
    coupon numeric(10,2) DEFAULT '0'::numeric,
    loyalty numeric(10,2) DEFAULT '0'::numeric,
    vendor_payout numeric(10,2) DEFAULT '0'::numeric,
    total_payments numeric(10,2),
    over_short numeric(10,2),
    departments jsonb DEFAULT '[]'::jsonb,
    total_transactions integer DEFAULT 0,
    last_updated_by character varying(16) DEFAULT 'bot'::character varying,
    last_updated_at timestamp without time zone DEFAULT now(),
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE public.daily_sales OWNER TO gasbot;

--
-- Name: daily_sales_id_seq; Type: SEQUENCE; Schema: public; Owner: gasbot
--

CREATE SEQUENCE public.daily_sales_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.daily_sales_id_seq OWNER TO gasbot;

--
-- Name: daily_sales_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: gasbot
--

ALTER SEQUENCE public.daily_sales_id_seq OWNED BY public.daily_sales.id;


--
-- Name: expenses; Type: TABLE; Schema: public; Owner: gasbot
--

CREATE TABLE public.expenses (
    id bigint NOT NULL,
    store_id character varying(64) NOT NULL,
    expense_date date NOT NULL,
    category character varying(64) NOT NULL,
    amount numeric(10,2) NOT NULL,
    notes text,
    last_updated_by character varying(16) DEFAULT 'bot'::character varying,
    last_updated_at timestamp without time zone DEFAULT now(),
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE public.expenses OWNER TO gasbot;

--
-- Name: expenses_id_seq; Type: SEQUENCE; Schema: public; Owner: gasbot
--

CREATE SEQUENCE public.expenses_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.expenses_id_seq OWNER TO gasbot;

--
-- Name: expenses_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: gasbot
--

ALTER SEQUENCE public.expenses_id_seq OWNED BY public.expenses.id;


--
-- Name: invoice_items; Type: TABLE; Schema: public; Owner: gasbot
--

CREATE TABLE public.invoice_items (
    id bigint NOT NULL,
    store_id character varying(64) NOT NULL,
    invoice_id integer,
    vendor character varying(128) NOT NULL,
    item_name character varying(256) NOT NULL,
    item_name_raw character varying(256) NOT NULL,
    upc character varying(32),
    unit_price numeric(10,4) NOT NULL,
    case_price numeric(10,4),
    case_qty integer,
    category character varying(64),
    invoice_date date NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    canonical_name character varying(256),
    confidence numeric(4,3)
);


ALTER TABLE public.invoice_items OWNER TO gasbot;

--
-- Name: invoice_items_id_seq; Type: SEQUENCE; Schema: public; Owner: gasbot
--

CREATE SEQUENCE public.invoice_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.invoice_items_id_seq OWNER TO gasbot;

--
-- Name: invoice_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: gasbot
--

ALTER SEQUENCE public.invoice_items_id_seq OWNED BY public.invoice_items.id;


--
-- Name: invoices; Type: TABLE; Schema: public; Owner: gasbot
--

CREATE TABLE public.invoices (
    id bigint NOT NULL,
    store_id character varying(64) NOT NULL,
    vendor character varying(128) NOT NULL,
    amount numeric(10,2) NOT NULL,
    invoice_date date NOT NULL,
    invoice_num character varying(64),
    line_items jsonb,
    matched_bank_transaction_id integer,
    last_updated_by character varying(16) DEFAULT 'bot'::character varying,
    last_updated_at timestamp without time zone DEFAULT now(),
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE public.invoices OWNER TO gasbot;

--
-- Name: invoices_id_seq; Type: SEQUENCE; Schema: public; Owner: gasbot
--

CREATE SEQUENCE public.invoices_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.invoices_id_seq OWNER TO gasbot;

--
-- Name: invoices_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: gasbot
--

ALTER SEQUENCE public.invoices_id_seq OWNED BY public.invoices.id;


--
-- Name: pending_state; Type: TABLE; Schema: public; Owner: gasbot
--

CREATE TABLE public.pending_state (
    id bigint NOT NULL,
    store_id character varying(64) NOT NULL,
    state_key character varying(64) NOT NULL,
    state_data jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE public.pending_state OWNER TO gasbot;

--
-- Name: pending_state_id_seq; Type: SEQUENCE; Schema: public; Owner: gasbot
--

CREATE SEQUENCE public.pending_state_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.pending_state_id_seq OWNER TO gasbot;

--
-- Name: pending_state_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: gasbot
--

ALTER SEQUENCE public.pending_state_id_seq OWNED BY public.pending_state.id;


--
-- Name: rebates; Type: TABLE; Schema: public; Owner: gasbot
--

CREATE TABLE public.rebates (
    id bigint NOT NULL,
    store_id character varying(64) NOT NULL,
    rebate_date date NOT NULL,
    vendor character varying(128) NOT NULL,
    amount numeric(10,2) NOT NULL,
    notes text,
    last_updated_by character varying(16) DEFAULT 'bot'::character varying,
    last_updated_at timestamp without time zone DEFAULT now(),
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE public.rebates OWNER TO gasbot;

--
-- Name: rebates_id_seq; Type: SEQUENCE; Schema: public; Owner: gasbot
--

CREATE SEQUENCE public.rebates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.rebates_id_seq OWNER TO gasbot;

--
-- Name: rebates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: gasbot
--

ALTER SEQUENCE public.rebates_id_seq OWNED BY public.rebates.id;


--
-- Name: revenues; Type: TABLE; Schema: public; Owner: gasbot
--

CREATE TABLE public.revenues (
    id bigint NOT NULL,
    store_id character varying(64) NOT NULL,
    revenue_date date NOT NULL,
    category character varying(64) NOT NULL,
    amount numeric(10,2) NOT NULL,
    notes text,
    last_updated_by character varying(16) DEFAULT 'bot'::character varying,
    last_updated_at timestamp without time zone DEFAULT now(),
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE public.revenues OWNER TO gasbot;

--
-- Name: revenues_id_seq; Type: SEQUENCE; Schema: public; Owner: gasbot
--

CREATE SEQUENCE public.revenues_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.revenues_id_seq OWNER TO gasbot;

--
-- Name: revenues_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: gasbot
--

ALTER SEQUENCE public.revenues_id_seq OWNED BY public.revenues.id;


--
-- Name: store_health_scores; Type: TABLE; Schema: public; Owner: gasbot
--

CREATE TABLE public.store_health_scores (
    id bigint NOT NULL,
    store_id character varying(64) NOT NULL,
    week_start date NOT NULL,
    score integer NOT NULL,
    over_short_avg numeric(10,2) DEFAULT '0'::numeric,
    expense_ratio numeric(5,4) DEFAULT '0'::numeric,
    invoice_match_rate numeric(5,4) DEFAULT '0'::numeric,
    details jsonb DEFAULT '{}'::jsonb,
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE public.store_health_scores OWNER TO gasbot;

--
-- Name: store_health_scores_id_seq; Type: SEQUENCE; Schema: public; Owner: gasbot
--

CREATE SEQUENCE public.store_health_scores_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.store_health_scores_id_seq OWNER TO gasbot;

--
-- Name: store_health_scores_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: gasbot
--

ALTER SEQUENCE public.store_health_scores_id_seq OWNED BY public.store_health_scores.id;


--
-- Name: vendor_prices; Type: TABLE; Schema: public; Owner: gasbot
--

CREATE TABLE public.vendor_prices (
    id bigint NOT NULL,
    store_id character varying(64) NOT NULL,
    vendor character varying(128) NOT NULL,
    category character varying(64),
    amount numeric(10,2) NOT NULL,
    invoice_date date NOT NULL,
    invoice_id integer,
    created_at timestamp without time zone DEFAULT now()
);


ALTER TABLE public.vendor_prices OWNER TO gasbot;

--
-- Name: vendor_prices_id_seq; Type: SEQUENCE; Schema: public; Owner: gasbot
--

CREATE SEQUENCE public.vendor_prices_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.vendor_prices_id_seq OWNER TO gasbot;

--
-- Name: vendor_prices_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: gasbot
--

ALTER SEQUENCE public.vendor_prices_id_seq OWNED BY public.vendor_prices.id;


--
-- Name: bank_transactions id; Type: DEFAULT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.bank_transactions ALTER COLUMN id SET DEFAULT nextval('public.bank_transactions_id_seq'::regclass);


--
-- Name: conversation_history id; Type: DEFAULT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.conversation_history ALTER COLUMN id SET DEFAULT nextval('public.conversation_history_id_seq'::regclass);


--
-- Name: daily_sales id; Type: DEFAULT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.daily_sales ALTER COLUMN id SET DEFAULT nextval('public.daily_sales_id_seq'::regclass);


--
-- Name: expenses id; Type: DEFAULT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.expenses ALTER COLUMN id SET DEFAULT nextval('public.expenses_id_seq'::regclass);


--
-- Name: invoice_items id; Type: DEFAULT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.invoice_items ALTER COLUMN id SET DEFAULT nextval('public.invoice_items_id_seq'::regclass);


--
-- Name: invoices id; Type: DEFAULT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.invoices ALTER COLUMN id SET DEFAULT nextval('public.invoices_id_seq'::regclass);


--
-- Name: pending_state id; Type: DEFAULT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.pending_state ALTER COLUMN id SET DEFAULT nextval('public.pending_state_id_seq'::regclass);


--
-- Name: rebates id; Type: DEFAULT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.rebates ALTER COLUMN id SET DEFAULT nextval('public.rebates_id_seq'::regclass);


--
-- Name: revenues id; Type: DEFAULT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.revenues ALTER COLUMN id SET DEFAULT nextval('public.revenues_id_seq'::regclass);


--
-- Name: store_health_scores id; Type: DEFAULT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.store_health_scores ALTER COLUMN id SET DEFAULT nextval('public.store_health_scores_id_seq'::regclass);


--
-- Name: vendor_prices id; Type: DEFAULT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.vendor_prices ALTER COLUMN id SET DEFAULT nextval('public.vendor_prices_id_seq'::regclass);


--
-- Data for Name: alembic_version; Type: TABLE DATA; Schema: public; Owner: gasbot
--

COPY public.alembic_version (version_num) FROM stdin;
004
\.


--
-- Data for Name: bank_transactions; Type: TABLE DATA; Schema: public; Owner: gasbot
--

COPY public.bank_transactions (id, store_id, transaction_date, amount, description, category, transaction_type, plaid_transaction_id, matched_invoice_id, is_matched, last_updated_by, last_updated_at, created_at) FROM stdin;
\.


--
-- Data for Name: conversation_history; Type: TABLE DATA; Schema: public; Owner: gasbot
--

COPY public.conversation_history (id, store_id, role, content, created_at) FROM stdin;
\.


--
-- Data for Name: daily_sales; Type: TABLE DATA; Schema: public; Owner: gasbot
--

COPY public.daily_sales (id, store_id, sale_date, product_sales, lotto_in, lotto_online, sales_tax, gpi, grand_total, refunds, lotto_po, lotto_cr, food_stamp, cash_drop, card, check_amount, atm, pull_tab, coupon, loyalty, vendor_payout, total_payments, over_short, departments, total_transactions, last_updated_by, last_updated_at, created_at) FROM stdin;
1	moraine	2026-03-16	1618.16	200.00	14.00	106.23	36.39	1974.78	0.00	52.00	35.00	25.39	703.00	904.65	0.00	0.00	0.00	0.00	63.05	221.00	\N	\N	[{"name": "Beer", "items": 67, "sales": 195.43}, {"name": "Cigarettes", "items": 71, "sales": 461.32}, {"name": "Dairy", "items": 1, "sales": 1.99}, {"name": "Grocery Non-Taxable", "items": 68, "sales": 165.43}, {"name": "Grocery taxable", "items": 12, "sales": 31.83}, {"name": "PAY IN", "items": 1, "sales": 54.36}, {"name": "Pizzza", "items": 1, "sales": 12.99}, {"name": "Pop", "items": 79, "sales": 239.08}, {"name": "pre roll", "items": 5, "sales": 26.95}, {"name": "Propain tank", "items": 1, "sales": 21.99}, {"name": "Tobacco", "items": 64, "sales": 220.02}, {"name": "Vape & Delta", "items": 7, "sales": 144.93}, {"name": "Wine and beer", "items": 16, "sales": 41.84}]	181	bot	2026-03-17 19:56:30.224578	2026-03-17 19:56:30.224578
\.


--
-- Data for Name: expenses; Type: TABLE DATA; Schema: public; Owner: gasbot
--

COPY public.expenses (id, store_id, expense_date, category, amount, notes, last_updated_by, last_updated_at, created_at) FROM stdin;
\.


--
-- Data for Name: invoice_items; Type: TABLE DATA; Schema: public; Owner: gasbot
--

COPY public.invoice_items (id, store_id, invoice_id, vendor, item_name, item_name_raw, upc, unit_price, case_price, case_qty, category, invoice_date, created_at, canonical_name, confidence) FROM stdin;
\.


--
-- Data for Name: invoices; Type: TABLE DATA; Schema: public; Owner: gasbot
--

COPY public.invoices (id, store_id, vendor, amount, invoice_date, invoice_num, line_items, matched_bank_transaction_id, last_updated_by, last_updated_at, created_at) FROM stdin;
\.


--
-- Data for Name: pending_state; Type: TABLE DATA; Schema: public; Owner: gasbot
--

COPY public.pending_state (id, store_id, state_key, state_data, created_at) FROM stdin;
2	moraine	invoice_items	{"items": [{"upc": "", "case_qty": null, "category": "CANDY", "item_name": "Boston Baked Beans Candy Coated Peanuts 0.35 24 CT", "case_price": null, "unit_price": 6.99, "item_name_raw": "Boston Baked Beans - Candy Coated Peanuts - 0.35 24 CT"}, {"upc": "", "case_qty": null, "category": "CANDY", "item_name": "Brach's Peppermint Candy Canes Jar 260 CT", "case_price": null, "unit_price": 15.99, "item_name_raw": "Brach's Peppermint Candy Canes Jar 260 CT"}, {"upc": "", "case_qty": null, "category": "CANDY", "item_name": "Laffy Taffy Rope Mystery Swirl 24 CT", "case_price": null, "unit_price": 8.99, "item_name_raw": "Laffy Taffy Rope - Mystery Swirl 24 CT"}, {"upc": "", "case_qty": null, "category": "CANDY", "item_name": "Lemonhead 0.35 24 CT", "case_price": null, "unit_price": 6.99, "item_name_raw": "Lemonhead - 0.35 24 CT"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Dutch Masters Tropical 2/1.29 60 CT", "case_price": null, "unit_price": 27.99, "item_name_raw": "DUTCH MASTERS - TROPICAL - 2/1.29 - 60 CT"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Swisher Banana Smash 2/1.39 30 CT", "case_price": null, "unit_price": 29.59, "item_name_raw": "SWISHER BANANA SMASH 2/1.39 - 30CT"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Swisher Black Cherry 2/1.49 15 PK", "case_price": null, "unit_price": 15.69, "item_name_raw": "SWISHER BLK CHERRY 2/1.49 - 15 PK"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Swisher Black Smooth 2/1.49 15 PK", "case_price": null, "unit_price": 15.69, "item_name_raw": "SWISHER BLK SMOOTH 2/1.49 - 15 PK"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Swisher Black Wine 2/1.49 15 CT", "case_price": null, "unit_price": 15.69, "item_name_raw": "SWISHER BLK WINE 2/1.49 - 15 CT"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Swisher Blueberry 2/1.39 30 CT", "case_price": null, "unit_price": 29.59, "item_name_raw": "SWISHER BLUEBERRY 2/1.39 - 30CT"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Swisher Cream 2/1.19 30 CT", "case_price": null, "unit_price": 27.99, "item_name_raw": "SWISHER CREAM 2/1.19 - 30CT"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Swisher Sweets 2/1.39 30 CT", "case_price": null, "unit_price": 29.59, "item_name_raw": "SWISHER SWEETS 2/1.39 - 30CT"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Swisher Tropical Fusion 2/1.39 30 CT", "case_price": null, "unit_price": 29.59, "item_name_raw": "SWISHER TROPICAL FUSION 2/1.39 - 30CT"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "White Owl Red White & Berry 2/1.19 2PK 30 CT", "case_price": null, "unit_price": 25.39, "item_name_raw": "WHITE OWL - RED, WHITE & BERRY - 2/1.19 - 2PK/30 CT"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "White Owl Sweets 2/1.19 2PK 30 CT", "case_price": null, "unit_price": 25.39, "item_name_raw": "WHITE OWL - SWEETS - 2/1.19 - 2PK/30 CT"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "White Owl White Grape 2/1.19 2PK 30 CT", "case_price": null, "unit_price": 25.39, "item_name_raw": "WHITE OWL - WHITE GRAPE - 2/1.19 - 2PK/30 CT"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Lil Leaf Dark 3/$2.99 10-3PK", "case_price": null, "unit_price": 19.49, "item_name_raw": "LIL LEAF - DARK - 3 /$2.99 - 10-3PK"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Lil Leaf Dark Red 3/$2.99 10-3PK", "case_price": null, "unit_price": 19.49, "item_name_raw": "LIL LEAF - DARK RED - 3 /$2.99 - 10-3PK"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Lil Leaf Dark White 3/$2.99 10-3PK", "case_price": null, "unit_price": 19.49, "item_name_raw": "LIL LEAF - DARK WHITE - 3 /$2.99 - 10-3PK"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Lil Leaf Gold 3/$2.99 10-3PK", "case_price": null, "unit_price": 19.49, "item_name_raw": "LIL LEAF - GOLD - 3 /$2.99 - 10-3PK"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Lil Leaf Mango 3/$2.99 10-3PK", "case_price": null, "unit_price": 19.49, "item_name_raw": "LIL LEAF - MANGO - 3 /$2.99 - 10-3PK"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Lil Leaf Russian Cream 3/$2.99 10-3PK", "case_price": null, "unit_price": 19.49, "item_name_raw": "LIL LEAF - RUSSIAN CREAM - 3 /$2.99 - 10-3PK"}, {"upc": "", "case_qty": null, "category": "TOBACCO", "item_name": "Lil Leaf White Rum 3/$2.99 10-3PK", "case_price": null, "unit_price": 19.49, "item_name_raw": "LIL LEAF - WHITE RUM - 3 /$2.99 - 10-3PK"}], "vendor": "SVV WHOLESALE LLC", "invoice_date": "2025-12-19", "invoice_number": "SO-251219-00075"}	2026-03-17 18:36:16.405069
\.


--
-- Data for Name: rebates; Type: TABLE DATA; Schema: public; Owner: gasbot
--

COPY public.rebates (id, store_id, rebate_date, vendor, amount, notes, last_updated_by, last_updated_at, created_at) FROM stdin;
\.


--
-- Data for Name: revenues; Type: TABLE DATA; Schema: public; Owner: gasbot
--

COPY public.revenues (id, store_id, revenue_date, category, amount, notes, last_updated_by, last_updated_at, created_at) FROM stdin;
\.


--
-- Data for Name: store_health_scores; Type: TABLE DATA; Schema: public; Owner: gasbot
--

COPY public.store_health_scores (id, store_id, week_start, score, over_short_avg, expense_ratio, invoice_match_rate, details, created_at) FROM stdin;
\.


--
-- Data for Name: vendor_prices; Type: TABLE DATA; Schema: public; Owner: gasbot
--

COPY public.vendor_prices (id, store_id, vendor, category, amount, invoice_date, invoice_id, created_at) FROM stdin;
\.


--
-- Name: bank_transactions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: gasbot
--

SELECT pg_catalog.setval('public.bank_transactions_id_seq', 1, false);


--
-- Name: conversation_history_id_seq; Type: SEQUENCE SET; Schema: public; Owner: gasbot
--

SELECT pg_catalog.setval('public.conversation_history_id_seq', 1, false);


--
-- Name: daily_sales_id_seq; Type: SEQUENCE SET; Schema: public; Owner: gasbot
--

SELECT pg_catalog.setval('public.daily_sales_id_seq', 1, true);


--
-- Name: expenses_id_seq; Type: SEQUENCE SET; Schema: public; Owner: gasbot
--

SELECT pg_catalog.setval('public.expenses_id_seq', 1, false);


--
-- Name: invoice_items_id_seq; Type: SEQUENCE SET; Schema: public; Owner: gasbot
--

SELECT pg_catalog.setval('public.invoice_items_id_seq', 1, false);


--
-- Name: invoices_id_seq; Type: SEQUENCE SET; Schema: public; Owner: gasbot
--

SELECT pg_catalog.setval('public.invoices_id_seq', 1, false);


--
-- Name: pending_state_id_seq; Type: SEQUENCE SET; Schema: public; Owner: gasbot
--

SELECT pg_catalog.setval('public.pending_state_id_seq', 2, true);


--
-- Name: rebates_id_seq; Type: SEQUENCE SET; Schema: public; Owner: gasbot
--

SELECT pg_catalog.setval('public.rebates_id_seq', 1, false);


--
-- Name: revenues_id_seq; Type: SEQUENCE SET; Schema: public; Owner: gasbot
--

SELECT pg_catalog.setval('public.revenues_id_seq', 1, false);


--
-- Name: store_health_scores_id_seq; Type: SEQUENCE SET; Schema: public; Owner: gasbot
--

SELECT pg_catalog.setval('public.store_health_scores_id_seq', 1, false);


--
-- Name: vendor_prices_id_seq; Type: SEQUENCE SET; Schema: public; Owner: gasbot
--

SELECT pg_catalog.setval('public.vendor_prices_id_seq', 1, false);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: bank_transactions bank_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.bank_transactions
    ADD CONSTRAINT bank_transactions_pkey PRIMARY KEY (id);


--
-- Name: bank_transactions bank_transactions_plaid_transaction_id_key; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.bank_transactions
    ADD CONSTRAINT bank_transactions_plaid_transaction_id_key UNIQUE (plaid_transaction_id);


--
-- Name: conversation_history conversation_history_pkey; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.conversation_history
    ADD CONSTRAINT conversation_history_pkey PRIMARY KEY (id);


--
-- Name: daily_sales daily_sales_pkey; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.daily_sales
    ADD CONSTRAINT daily_sales_pkey PRIMARY KEY (id);


--
-- Name: expenses expenses_pkey; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.expenses
    ADD CONSTRAINT expenses_pkey PRIMARY KEY (id);


--
-- Name: invoice_items invoice_items_pkey; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.invoice_items
    ADD CONSTRAINT invoice_items_pkey PRIMARY KEY (id);


--
-- Name: invoices invoices_pkey; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_pkey PRIMARY KEY (id);


--
-- Name: pending_state pending_state_pkey; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.pending_state
    ADD CONSTRAINT pending_state_pkey PRIMARY KEY (id);


--
-- Name: rebates rebates_pkey; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.rebates
    ADD CONSTRAINT rebates_pkey PRIMARY KEY (id);


--
-- Name: revenues revenues_pkey; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.revenues
    ADD CONSTRAINT revenues_pkey PRIMARY KEY (id);


--
-- Name: store_health_scores store_health_scores_pkey; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.store_health_scores
    ADD CONSTRAINT store_health_scores_pkey PRIMARY KEY (id);


--
-- Name: daily_sales uq_daily_sales_store_date; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.daily_sales
    ADD CONSTRAINT uq_daily_sales_store_date UNIQUE (store_id, sale_date);


--
-- Name: store_health_scores uq_health_store_week; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.store_health_scores
    ADD CONSTRAINT uq_health_store_week UNIQUE (store_id, week_start);


--
-- Name: pending_state uq_pending_store_key; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.pending_state
    ADD CONSTRAINT uq_pending_store_key UNIQUE (store_id, state_key);


--
-- Name: vendor_prices vendor_prices_pkey; Type: CONSTRAINT; Schema: public; Owner: gasbot
--

ALTER TABLE ONLY public.vendor_prices
    ADD CONSTRAINT vendor_prices_pkey PRIMARY KEY (id);


--
-- Name: ix_bank_transactions_store_id; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_bank_transactions_store_id ON public.bank_transactions USING btree (store_id);


--
-- Name: ix_conversation_history_store_id; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_conversation_history_store_id ON public.conversation_history USING btree (store_id);


--
-- Name: ix_daily_sales_store_id; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_daily_sales_store_id ON public.daily_sales USING btree (store_id);


--
-- Name: ix_expenses_store_id; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_expenses_store_id ON public.expenses USING btree (store_id);


--
-- Name: ix_invoice_items_canonical_name; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_invoice_items_canonical_name ON public.invoice_items USING btree (canonical_name);


--
-- Name: ix_invoice_items_invoice_date; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_invoice_items_invoice_date ON public.invoice_items USING btree (invoice_date);


--
-- Name: ix_invoice_items_item_name; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_invoice_items_item_name ON public.invoice_items USING btree (item_name);


--
-- Name: ix_invoice_items_store_id; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_invoice_items_store_id ON public.invoice_items USING btree (store_id);


--
-- Name: ix_invoice_items_upc; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_invoice_items_upc ON public.invoice_items USING btree (upc);


--
-- Name: ix_invoice_items_vendor; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_invoice_items_vendor ON public.invoice_items USING btree (vendor);


--
-- Name: ix_invoices_store_id; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_invoices_store_id ON public.invoices USING btree (store_id);


--
-- Name: ix_pending_state_store_id; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_pending_state_store_id ON public.pending_state USING btree (store_id);


--
-- Name: ix_rebates_store_id; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_rebates_store_id ON public.rebates USING btree (store_id);


--
-- Name: ix_revenues_store_id; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_revenues_store_id ON public.revenues USING btree (store_id);


--
-- Name: ix_store_health_scores_store_id; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_store_health_scores_store_id ON public.store_health_scores USING btree (store_id);


--
-- Name: ix_vendor_prices_store_id; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_vendor_prices_store_id ON public.vendor_prices USING btree (store_id);


--
-- Name: ix_vendor_prices_vendor; Type: INDEX; Schema: public; Owner: gasbot
--

CREATE INDEX ix_vendor_prices_vendor ON public.vendor_prices USING btree (vendor);


--
-- PostgreSQL database dump complete
--

\unrestrict gTdRrRKwfMVZXr4kK1VOnkzvZYsBIggpWqMaRVWS6P9U8pue3RjOaBma4KPAt3r


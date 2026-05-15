import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import streamlit as st
load_dotenv(override=True)


def get_db_url():
    # 1. Check Streamlit Cloud Secrets ONLY if the file exists or we are in the Cloud
    # We check if the internal secrets object is actually populated
    try:
        if st.secrets.load_if_toml_exists() and "PGHOST" in st.secrets:
            s = st.secrets
            return (
                f"postgresql+psycopg2://{s['PGUSER']}:{s['PGPASSWORD']}"
                f"@{s['PGHOST']}:{s['PGPORT']}/{s['PGDATABASE']}"
                f"?sslmode=require"
            )
    except Exception:
        # If st.secrets raises any error locally, just pass through to Step 2
        pass

    # 2. Fallback to os.getenv for your local PC (HQ-PC-29)
    # This is where your .env file data will be used
    return (
        f"postgresql+psycopg2://{os.getenv('PGUSER')}:{os.getenv('PGPASSWORD')}"
        f"@{os.getenv('PGHOST')}:{os.getenv('PGPORT')}/{os.getenv('PGDATABASE')}"
        f"?sslmode=require"
    )


DB_URL = get_db_url()

def get_db_connection():
    """
    Returns a SQLAlchemy Engine.
    In a Streamlit context, you'll wrap this call in @st.cache_resource.
    """
    engine = create_engine(
        DB_URL,
        pool_size=10,           # Keep 10 connections open
        max_overflow=20,        # Allow 20 more during rush hour
        pool_recycle=300,       # Azure kills idle connections; this resets them first
        pool_pre_ping=True      # Transparently reconnects if the pipe is broken
    )
    return engine


def create_tables():
    """Uses the engine to execute the schema setup."""
    engine = get_db_connection()

    # We use engine.begin() so it automatically commits at the end
    with engine.begin() as conn:
        # --- PART 1: CONFIGURATION TABLES ---
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS revenue_centers (
                guid UUID PRIMARY KEY,
                name TEXT,
                description TEXT
            );
        '''))

        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS dining_options (
                guid UUID PRIMARY KEY,
                name TEXT,
                behavior TEXT
            );
        '''))

        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS services (
                guid UUID PRIMARY KEY,
                name TEXT
            );
        '''))

        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS employees (
                guid UUID PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                email TEXT,
                deleted BOOLEAN DEFAULT FALSE
            );
        '''))

        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS sales_categories (
                guid UUID PRIMARY KEY,
                name TEXT
            );
        '''))

        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS service_areas (
                guid UUID PRIMARY KEY,
                name TEXT,
                revenue_center_guid UUID REFERENCES revenue_centers(guid) ON DELETE SET NULL
            );
        '''))

        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS tables (
                guid UUID PRIMARY KEY,
                name TEXT,
                revenue_center_guid UUID REFERENCES revenue_centers(guid) ON DELETE SET NULL,
                service_area_guid UUID REFERENCES service_areas(guid) ON DELETE SET NULL
            );
        '''))

        # --- PART 2: TRANSACTIONAL TABLES (THE 4 TIERS) ---

        # TIER 1: ORDER HEADER
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS orders_head (
                order_guid UUID PRIMARY KEY,
                location_id TEXT,
                order_number TEXT,
                fire_date TIMESTAMPTZ,
                promised_date TIMESTAMPTZ,
                created_date TIMESTAMPTZ,
                closed_date TIMESTAMPTZ,
                paid_date TIMESTAMPTZ,
                modified_date TIMESTAMPTZ,
                deleted_date TIMESTAMPTZ,
                estimated_fulfillment_date TIMESTAMPTZ,
                business_date INTEGER,
                required_prep_time TEXT,
                number_of_guests INTEGER,
                approval_status TEXT,
                deleted BOOLEAN DEFAULT FALSE,
                source TEXT,
                dining_option_guid UUID REFERENCES dining_options(guid),
                service_area_guid UUID REFERENCES service_areas(guid),
                restaurant_service_daypart UUID REFERENCES services(guid), 
                revenue_center_guid UUID REFERENCES revenue_centers(guid),
                server_guid UUID REFERENCES employees(guid),
                last_sync_timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        '''))

        # TIER 2: CHECKS
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS order_checks (
                check_guid UUID PRIMARY KEY,
                order_guid UUID REFERENCES orders_head(order_guid) ON DELETE CASCADE,
                payment_status TEXT,
                tax_exempt BOOLEAN DEFAULT FALSE,
                total_amount NUMERIC(12,2),
                tax_amount NUMERIC(12,2),
                net_amount NUMERIC(12,2),
                tab_name TEXT,
                customer_first TEXT,
                customer_last TEXT,
                customer_phone TEXT,
                customer_email TEXT,
                opened_date TIMESTAMPTZ,
                closed_date TIMESTAMPTZ,
                voided BOOLEAN DEFAULT FALSE
            );
        '''))

        # TIER 3: ITEMS
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS order_items (
                selection_guid UUID PRIMARY KEY,
                check_guid UUID REFERENCES order_checks(check_guid) ON DELETE CASCADE,
                item_guid UUID,
                item_name TEXT,
                quantity NUMERIC(12,3),
                unit_price NUMERIC(12,2),
                net_price NUMERIC(12,2),
                deferred BOOLEAN DEFAULT FALSE,
                tax_amount NUMERIC(12,2),
                voided BOOLEAN DEFAULT FALSE,
                fulfillment_status TEXT,
                plu TEXT,
                sales_category_guid UUID REFERENCES sales_categories(guid)
            );
        '''))

        # TIER 4: MODIFIERS
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS item_modifiers (
                modifier_guid UUID PRIMARY KEY,
                selection_guid UUID REFERENCES order_items(selection_guid) ON DELETE CASCADE,
                item_guid UUID,
                mod_name TEXT,
                quantity NUMERIC(12,3),
                mod_unit_price NUMERIC(12,2),
                mod_net_price NUMERIC(12,2),
                deferred BOOLEAN DEFAULT FALSE,
                voided BOOLEAN DEFAULT FALSE
            );
        '''))


        print("Azure Postgres: All tables (Configs + 4-Tier Orders) verified.")


def rebuild_database():
    """Drops existing tables and recreates them."""
    engine = get_db_connection()
    tables = [
        "item_modifiers", "order_items", "order_checks", "orders_head",
        "tables", "service_areas", "sales_categories", "employees",
        "services", "dining_options", "revenue_centers"
    ]

    with engine.begin() as conn:
        print("Dropping all existing tables...")
        for table in tables:
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE;"))

    create_tables()
    print("Azure Postgres database successfully reconstructed.")

if __name__ == "__main__":
    rebuild_database()
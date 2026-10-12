-- Hermes — reference / seed data (0002_seed)
-- Idempotent: ON CONFLICT DO NOTHING so re-running is safe. Seeds only stable
-- reference rows (roles, the 8 businesses, the app catalog) — never user data.

-- ---- roles ----------------------------------------------------------------
INSERT INTO roles (name, description) VALUES
    ('owner',     'Full access across all businesses, including approvals.'),
    ('admin',     'Manage one or more businesses; cannot change global settings.'),
    ('read-only', 'View data only; cannot trigger writes or approvals.')
ON CONFLICT (name) DO NOTHING;

-- ---- entities (the 8 businesses) ------------------------------------------
-- Gandi seed removed for Sawera's assistant (PLAN.md §2.0 step 9): projects
-- are created from the UI/API, nothing project-specific lives in code or seed
-- data. First launch = empty registry until the first project is created.

-- ---- connected_apps (the app catalog) -------------------------------------
-- Slugs match HERMES_APPS so the agent's tool layer and the DB agree.
INSERT INTO connected_apps (slug, name, category, provider) VALUES
    ('quickbooks',      'QuickBooks',       'accounting',    'Intuit'),
    ('pdf-co',          'PDF.co',           'documents',     'PDF.co'),
    ('google-contacts', 'Google Contacts',  'contacts',      'Google'),
    ('fathom',          'Fathom',           'meetings',      'Fathom'),
    ('google-meet',     'Google Meet',      'meetings',      'Google'),
    ('google-forms',    'Google Forms',     'forms',         'Google'),
    ('google-sheets',   'Google Sheets',    'spreadsheets',  'Google'),
    ('google-docs',     'Google Docs',      'documents',     'Google'),
    ('google-drive',    'Google Drive',     'storage',       'Google'),
    ('google-calendar', 'Google Calendar',  'calendar',      'Google'),
    ('google-mail',     'Gmail',            'email',         'Google'),
    ('notion',          'Notion',           'productivity',  'Notion')
ON CONFLICT (slug) DO NOTHING;

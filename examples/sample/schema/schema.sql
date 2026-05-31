-- Sample schema for phantomlint demo (generic e-commerce, fictional).

CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL,
  name TEXT
);

CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  total REAL,
  status TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Near-miss: the API queries `payments` (plural) but the schema only defines
-- `payment` (singular) -> phantom table with a fuzzy "did you mean `payment`?".
CREATE TABLE payment (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL,
  amount REAL,
  FOREIGN KEY (order_id) REFERENCES orders(id)
);

-- Defined but never queried by the API -> dead table.
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  action TEXT,
  created_at TEXT
);

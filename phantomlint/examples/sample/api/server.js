// Sample API worker for phantomlint demo. Hand-rolled routing (3 styles).

export default {
  async fetch(request, env) {
    const { pathname: path } = new URL(request.url);
    const db = env.DB;

    // exact route
    if (path === "/api/users") {
      return json(db.prepare("SELECT id, email, name FROM users WHERE id = ?").bind(1).first());
    }

    // regex route
    const orderMatch = path.match(/^\/api\/orders\/(\d+)$/);
    if (orderMatch) {
      return json(db.prepare("SELECT * FROM orders WHERE id = ?").bind(orderMatch[1]).first());
    }

    // create order - NOTE: coupon_code column does not exist in schema -> phantom column
    if (path === "/api/orders" && request.method === "POST") {
      db.prepare("INSERT INTO orders (user_id, total, status, coupon_code) VALUES (?, ?, ?, ?)")
        .bind(1, 9.99, "new", "SAVE10").run();
      return json({ ok: true });
    }

    // queries `payments` but the schema only defines `payment` -> phantom table
    if (path === "/api/balance") {
      return json(db.prepare("SELECT amount FROM payments WHERE user_id = ?").bind(1).first());
    }

    return new Response("not found", { status: 404 });
  },
};

function json(x) {
  return new Response(JSON.stringify(x), { headers: { "content-type": "application/json" } });
}

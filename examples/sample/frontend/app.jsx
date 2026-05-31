// Sample frontend for phantomlint demo.
import { useEffect, useState } from "react";

export default function App({ id }) {
  const [data, setData] = useState(null);

  useEffect(() => {
    fetch("/api/users").then((r) => r.json());            // served
    fetch(`/api/orders/${id}`).then((r) => r.json());     // served (regex route)
    fetch("/api/refunds").then((r) => r.json());          // NOT served -> phantom endpoint
    // fetch("/api/legacy-export");                        // commented out -> NOT a phantom
  }, [id]);

  return <div>{JSON.stringify(data)}</div>;
}

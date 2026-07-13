import { getAllGatewayModels, getCapabilities, isDemo } from "@/lib/ai/models";
// CTO-170: serve the discovery-RESOLVED curated lineup (server-only module) so
// the client picker self-heals to the current SKUs. Falls back to the pinned
// literals offline. Capabilities are keyed to these resolved ids.
import { chatModels } from "@/lib/ai/models.server";

export async function GET() {
  const headers = {
    "Cache-Control": "public, max-age=86400, s-maxage=86400",
  };

  const curatedCapabilities = await getCapabilities(chatModels);

  if (isDemo) {
    const models = await getAllGatewayModels();
    const capabilities = Object.fromEntries(
      models.map((m) => [m.id, curatedCapabilities[m.id] ?? m.capabilities])
    );

    return Response.json({ capabilities, models }, { headers });
  }

  // Return the resolved curated lineup alongside capabilities so the picker
  // renders self-healed ids. The client tolerates both this `{capabilities,
  // models}` shape and a bare capabilities map (see multimodal-input.tsx).
  return Response.json(
    { capabilities: curatedCapabilities, models: chatModels },
    { headers }
  );
}

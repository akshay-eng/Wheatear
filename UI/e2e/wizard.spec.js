import { expect, test } from "@playwright/test";
import path from "node:path";

const n8nFixture = path.resolve(
  "../engine/wheatear/connectors/n8n/fixtures/supervisor.json",
);

function mockTarget(page) {
  return page.route("**/api/target/validate", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      message: "Connected to watsonx Orchestrate. 4 agents currently on target.",
      agent_count: 4,
    }),
  }));
}

function mockCompletedJob(page) {
  return Promise.all([
    page.route("**/api/jobs", (route) => route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ id: "12345678browser", status: "queued" }),
    })),
    page.route("**/api/jobs/12345678browser/events", (route) => route.fulfill({
      status: 200,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
      },
      body: [
        "id: 0",
        "event: log",
        `data: ${JSON.stringify({
          id: 0,
          timestamp: "10:04:01",
          stage: "extract",
          level: "ok",
          message: "Parsed the source graph.",
        })}`,
        "",
        "id: 1",
        "event: log",
        `data: ${JSON.stringify({
          id: 1,
          timestamp: "10:04:02",
          stage: "deploy",
          level: "ok",
          message: "Supervisor: deployed.",
        })}`,
        "",
        "event: done",
        `data: ${JSON.stringify({
          status: "completed",
          download: true,
          summary: {
            processed: 3,
            deployed: 3,
            failed: 0,
            manual_steps: 0,
            dry_run: false,
            agents: [
              {
                id: "agent-1",
                name: "Supervisor",
                deployed: true,
                detail: "Imported",
                tools: ["web_search", "workflow_call"],
                dropped: [],
              },
              {
                id: "agent-2",
                name: "Research Agent",
                deployed: true,
                detail: "Imported",
                tools: ["web_search"],
                dropped: [],
              },
              {
                id: "agent-3",
                name: "Writing Agent",
                deployed: true,
                detail: "Imported",
                tools: [],
                dropped: [],
              },
            ],
            follow_up: [],
            pending_tools: [],
            connection_reviews: [
              {
                app_id: "servicenow_prod",
                environment: "draft",
                tools: ["web_search"],
                ready: true,
                configured: true,
                credentials_entered: true,
                preference: "team",
                security_scheme: "basic_auth",
                server_url: "https://contoso.service-now.com",
                summary: "One shared credential is ready on the target.",
                default_kind: "basic_auth",
                auth_options: [
                  {
                    value: "basic_auth",
                    label: "Username and password",
                    fields: [
                      { name: "username", label: "Username", secret: false },
                      { name: "password", label: "Password", secret: true },
                    ],
                  },
                  {
                    value: "bearer_token",
                    label: "Bearer token",
                    fields: [
                      { name: "token", label: "Bearer token", secret: true },
                    ],
                  },
                ],
              },
            ],
            documents: [
              {
                name: "evaluation-prompts.md",
                path: "evaluation/evaluation-prompts.md",
                label: "Evaluation prompts",
              },
              {
                name: "business-summary.md",
                path: "evaluation/business-summary.md",
                label: "Business summary",
              },
              {
                name: "migration-mapping.md",
                path: "evaluation/migration-mapping.md",
                label: "Source-to-target mapping",
              },
            ],
          },
        })}`,
        "",
        "",
      ].join("\n"),
    })),
  ]);
}

test("n8n upload follows the complete six-stage flow", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "full corridor is exercised once");

  await page.route("**/api/uploads", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      upload_id: "browser-fixture",
      message: "Found 1 item in the upload.",
      items: [{
        id: "h6wAloIJ62rKQMQM",
        name: "Supervisor",
        description: "Active",
        active: true,
        kind: "workflow",
        source_id: "h6wAloIJ62rKQMQM",
      }],
    }),
  }));
  await mockTarget(page);
  await mockCompletedJob(page);
  await page.route("**/api/connections/configure", async (route) => {
    const payload = route.request().postDataJSON();
    expect(payload.app_id).toBe("servicenow_prod");
    expect(payload.credentials).toEqual({
      username: "presentation-user",
      password: "presentation-password",
    });
    expect(payload.target.api_key).toBe("browser-only-secret");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        message: "Configured servicenow_prod.",
        actions: ["stored the credential"],
        connection: { ready: true },
      }),
    });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Choose the migration corridor" })).toBeVisible();
  await page.getByRole("radio", { name: /n8n/ }).click();
  await page.getByRole("button", { name: "Export files" }).click();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Set the delivery policy" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Update existing" }))
    .toHaveAttribute("data-selected", "true");
  await page.getByRole("button", { name: "Continue" }).click();

  await page.locator('input[type="file"]').setInputFiles(n8nFixture);
  await page.getByLabel("Service instance URL").fill("https://example.invalid/instances/demo");
  await page.getByLabel("IBM Cloud API key", { exact: true }).fill("browser-only-secret");
  await page.getByRole("button", { name: "Read export" }).click();
  await page.getByRole("button", { name: "Test target" }).click();
  await expect(page.getByText("Connected", { exact: true })).toHaveCount(2);
  await page.getByRole("button", { name: "Continue" }).click();

  await expect(page.getByText("Supervisor", { exact: true })).toBeVisible();
  await page.getByRole("checkbox", { name: /Supervisor/ }).click();
  await page.getByRole("button", { name: "Continue" }).click();

  const storage = await page.evaluate(() => ({
    session: sessionStorage.getItem("agent-liftoff.migration.session.v1"),
    local: localStorage.getItem("agent-liftoff.migration.session.v1"),
  }));
  expect(storage.session).toContain("browser-only-secret");
  expect(storage.local).toBeNull();

  await expect(page.getByRole("heading", { name: "Translation and preflight" })).toBeVisible();
  await page.getByLabel("Translation model provider").selectOption("watsonx");
  await page.getByLabel("IBM watsonx Orchestrate API key", { exact: true })
    .fill("ibm-labeled-key");
  await expect(
    page.getByRole("definition").filter({ hasText: "IBM watsonx Orchestrate" }),
  ).toHaveText("IBM watsonx Orchestrate");
  await page.getByRole("button", { name: "Continue" }).click();
  await page.screenshot({ path: "test-results/liftoff-ready-desktop.png", fullPage: true });
  const migrationRequest = page.waitForRequest("**/api/jobs");
  await page.getByRole("button", { name: "Start migration" }).click();
  const migrationPayload = (await migrationRequest).postDataJSON();
  expect(migrationPayload.translation).toEqual({
    provider: "watsonx",
    api_key: "ibm-labeled-key",
  });
  await expect(page.getByText("Review connection credentials")).toBeVisible();
  await expect(page.getByText("Supervisor: deployed.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Migration results" })).toBeVisible();
  await expect(page.getByText("3 agents")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Connection credentials" })).toBeVisible();
  await expect(page.getByText("0 of 1 reviewed")).toBeVisible();
  await page.getByRole("button", { name: "Use different credentials" }).click();
  await page.screenshot({
    path: "test-results/liftoff-credential-review-desktop.png",
    fullPage: true,
  });
  await page.getByLabel("Username for servicenow_prod").fill("presentation-user");
  await page.getByLabel("Password for servicenow_prod", { exact: true })
    .fill("presentation-password");
  await page.getByRole("button", { name: "Apply once" }).click();
  await expect(page.getByText("Configured servicenow_prod.")).toBeVisible();
  await expect(page.getByText("1 of 1 reviewed")).toBeVisible();
  await expect(page.getByText("Migration complete")).toBeVisible();
  await page.getByRole("button", { name: "Change" }).click();
  await expect(page.getByText("0 of 1 reviewed")).toBeVisible();
  await expect(page.getByText("Review connection credentials")).toBeVisible();
  await page.getByRole("button", { name: "Use existing credential" }).click();
  await expect(page.getByText("1 of 1 reviewed")).toBeVisible();
  await expect(page.getByText("Migration complete")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Evaluation pack" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Evaluation prompts/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /Business summary/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /Source-to-target mapping/ })).toBeVisible();
  await expect(page.getByRole("link", { name: "Download results" })).toBeVisible();
  await page.screenshot({ path: "test-results/liftoff-complete-desktop.png", fullPage: true });
});

test("Copilot live discovery scans solutions before listing agents", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "solution hierarchy is exercised once");

  let authPolls = 0;
  await page.route("**/api/copilot/auth/sessions", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      id: "browser-auth-session",
      status: "pending",
      user_code: "ABCD-EFGH",
      verification_uri: "https://microsoft.com/devicelogin",
      environments: [],
      expires_in: 900,
    }),
  }));
  await page.route("**/api/copilot/auth/sessions/browser-auth-session", (route) => {
    authPolls += 1;
    const authenticated = authPolls > 1;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(authenticated
        ? {
          id: "browser-auth-session",
          status: "authenticated",
          account_name: "maker@contoso.com",
          environments: [
            { id: "environment-dev", name: "Contoso Development" },
            { id: "environment-prod", name: "Contoso Production" },
          ],
          expires_in: 7200,
        }
        : {
          id: "browser-auth-session",
          status: "pending",
          user_code: "ABCD-EFGH",
          verification_uri: "https://microsoft.com/devicelogin",
          environments: [],
          expires_in: 899,
        }),
    });
  });
  await page.route("**/api/discover", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      message: "Connected to Dataverse. Found 2 unmanaged solutions.",
      items: [
        {
          id: "customer_service",
          name: "Customer Service Agents",
          description: "customer_service",
          version: "2.1.0.0",
          kind: "solution",
          source_id: "solution-1",
        },
        {
          id: "hr_archive",
          name: "HR Archive",
          description: "hr_archive",
          version: "1.0.0.0",
          kind: "solution",
          source_id: "solution-2",
        },
      ],
    }),
  }));
  await page.route("**/api/copilot/scan", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      scan_id: "solution-scan",
      message: "Scanned 1 solution. Found 2 agents.",
      items: [
        {
          id: "customer_service::contoso_Main",
          name: "Main Orchestrator",
          description: "Schema: contoso_Main",
          source_id: "contoso_Main",
          group_id: "customer_service",
          group_name: "Customer Service Agents",
          version: "2.1.0.0",
          kind: "agent",
        },
        {
          id: "customer_service::contoso_Sales",
          name: "Sales Assistant",
          description: "Schema: contoso_Sales",
          source_id: "contoso_Sales",
          group_id: "customer_service",
          group_name: "Customer Service Agents",
          version: "2.1.0.0",
          kind: "agent",
        },
      ],
    }),
  }));
  await mockTarget(page);

  await page.goto("/");
  await page.getByRole("radio", { name: /Microsoft Copilot Studio/ }).click();
  await page.getByRole("button", { name: "Continue" }).click();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByLabel("Dataverse environment URL")).toHaveCount(0);
  await expect(page.getByLabel("Dataverse access token")).toHaveCount(0);
  await page.getByRole("button", { name: "Sign in with Microsoft" }).click();
  await expect(page.getByText("ABCD-EFGH")).toBeVisible();
  await expect(page.getByText("maker@contoso.com")).toBeVisible();
  await page.getByLabel("Search Power Platform environments").fill("production");
  await page.getByRole("radio", { name: "Contoso Production environment" }).click();
  await page.screenshot({
    path: "test-results/liftoff-copilot-authenticated.png",
    fullPage: true,
  });
  await page.getByLabel("Service instance URL").fill("https://example.invalid/instances/demo");
  await page.getByLabel("IBM Cloud API key", { exact: true }).fill("ibm-session-key");
  const discoverRequest = page.waitForRequest("**/api/discover");
  await page.getByRole("button", { name: "Discover solutions" }).click();
  const sourcePayload = (await discoverRequest).postDataJSON().source;
  expect(sourcePayload.auth_session_id).toBe("browser-auth-session");
  expect(sourcePayload.environment_id).toBe("environment-prod");
  expect(sourcePayload.environment_url).toBeUndefined();
  expect(sourcePayload.access_token).toBeUndefined();
  await page.getByRole("button", { name: "Test target" }).click();
  await page.getByRole("button", { name: "Continue" }).click();

  await page.getByLabel("Search solution name or unique name").fill("customer");
  await expect(page.getByText("HR Archive", { exact: true })).toBeHidden();
  await page.getByRole("checkbox", { name: /Customer Service Agents/ }).click();
  await page.getByRole("button", { name: "Scan selected solutions" }).click();
  await expect(page.getByText("Scanned 1 solution. Found 2 agents.")).toBeVisible();
  await page.getByLabel("Search agent name, schema or solution").fill("orchestrator");
  await page.getByRole("checkbox", { name: /Main Orchestrator/ }).click();
  const agentSelectionTitle = page
    .getByRole("heading", { name: "Select agents" })
    .locator("..");
  await expect(agentSelectionTitle.getByText("1 of 2 selected")).toBeVisible();
  await page.screenshot({ path: "test-results/liftoff-copilot-selection.png", fullPage: true });
});

test("mobile source step has no horizontal overflow", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile layout is exercised once");
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Choose the migration corridor" })).toBeVisible();
  await page.getByRole("radio", { name: /Microsoft Copilot Studio/ }).click();
  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
  }));
  expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
  await page.screenshot({ path: "test-results/liftoff-source-mobile.png", fullPage: true });
});

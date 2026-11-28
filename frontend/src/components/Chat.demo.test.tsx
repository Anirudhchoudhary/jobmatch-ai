import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Chat } from "./Chat";
import { BackendProvider } from "@/lib/backend";
import { createDemoBackend, DEMO_FREE_TEXT_ANSWER, type DemoRecording } from "@/lib/demoBackend";
import recording from "@/demo/recording.json";

const rec = recording as unknown as DemoRecording;

function renderDemo() {
  return render(
    <BackendProvider backend={createDemoBackend(rec, { step: 1, token: 1 })}>
      <Chat />
    </BackendProvider>,
  );
}

describe("Chat in the demo", () => {
  it("keeps the questions not yet asked available after an answer", async () => {
    renderDemo();
    const [first, second] = rec.chats.map((c) => c.question);
    await userEvent.click(screen.getByRole("button", { name: first }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
    await waitFor(() => expect(screen.queryByRole("button", { name: first })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: second })).toBeInTheDocument();
  });

  it("tells visitors to use the suggested questions, and answers free text without a stuck working line", async () => {
    renderDemo();
    const input = screen.getByLabelText("Ask a question");
    expect(input).toHaveAttribute("placeholder", expect.stringMatching(/suggested questions/i));
    await userEvent.type(input, "What is my salary?{enter}");
    await waitFor(() => expect(screen.getAllByText(DEMO_FREE_TEXT_ANSWER).length).toBeGreaterThan(0));
    expect(screen.queryByText(/Searching your documents/)).not.toBeInTheDocument();
  });
});

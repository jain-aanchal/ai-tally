import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import HomePage from "@/app/page";

describe("HomePage", () => {
  it("renders the four dashboard cards", () => {
    render(<HomePage />);
    expect(screen.getByText("Spend — last 30 days")).toBeDefined();
    expect(screen.getByText("Top cost outliers (24h)")).toBeDefined();
    expect(screen.getByText("ROI snapshot")).toBeDefined();
    expect(screen.getByText("Data quality")).toBeDefined();
  });

  it("shows total spend formatted", () => {
    render(<HomePage />);
    expect(screen.getByText("$14,820.00")).toBeDefined();
  });

  it("marks an unattributed feature", () => {
    render(<HomePage />);
    expect(screen.getByText("unattributed")).toBeDefined();
  });
});

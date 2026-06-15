import { createRoot } from "react-dom/client";

export function renderReactComponent(
  component: React.ReactElement,
  dom: HTMLElement
) {
  const root = createRoot(dom);
  root.render(component);

  return {
    destroy: () => root.unmount(),
  };
}

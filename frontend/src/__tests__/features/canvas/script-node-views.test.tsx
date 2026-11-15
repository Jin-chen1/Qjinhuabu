// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ReactFlowProvider, type NodeProps } from '@xyflow/react';
import { afterAll, beforeAll, beforeEach, expect, it, vi } from 'vitest';

import { CANVAS_NODE_TYPES, type ScriptNodeData } from '@/features/canvas/domain/canvasNodes';
import { ScriptNode } from '@/features/canvas/nodes/ScriptNode';
import { useCanvasStore } from '@/stores/canvasStore';

beforeAll(() => vi.stubGlobal('ResizeObserver', class {
  observe() {}
  unobserve() {}
  disconnect() {}
}));
afterAll(() => vi.unstubAllGlobals());

function StoredScriptNode() {
  const data = useCanvasStore((state) => state.nodes[0].data) as ScriptNodeData;
  return <ScriptNode {...({
    id: 'script',
    type: CANVAS_NODE_TYPES.script,
    data,
    selected: false,
  } as NodeProps & { data: ScriptNodeData })} />;
}

function renderScript() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ReactFlowProvider>
        <StoredScriptNode />
      </ReactFlowProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useCanvasStore.setState({
    nodes: [{
      id: 'script',
      type: CANVAS_NODE_TYPES.script,
      position: { x: 0, y: 0 },
      data: {
        scriptResult: {
          title: '眼镜展示',
          rows: [{ shot_no: 1, duration: 3, visual_description: '黑框眼镜棚拍' }],
        },
      },
    }],
    edges: [],
    selectedNodeId: null,
  });
});

it('opens the view menu and retains the selected view after remounting', async () => {
  const user = userEvent.setup();
  const view = renderScript();
  expect(screen.getByRole('table')).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: '脚本视图' }));
  expect(await screen.findByRole('menuitemradio', { name: '表格视图' }))
    .toHaveAttribute('aria-checked', 'true');
  await user.click(screen.getByRole('menuitemradio', { name: '卡片视图' }));
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
  expect(screen.getByRole('article')).toBeInTheDocument();

  view.unmount();
  renderScript();
  expect(screen.getByRole('button', { name: '卡片视图' })).toBeInTheDocument();
  expect(screen.getByRole('article')).toBeInTheDocument();
});

it('shares card edits with the fullscreen table without changing other row fields', async () => {
  const user = userEvent.setup();
  renderScript();
  await user.click(screen.getByRole('button', { name: '脚本视图' }));
  await user.click(await screen.findByRole('menuitemradio', { name: '卡片视图' }));
  const field = screen.getByRole('article').querySelectorAll('[contenteditable="true"]')[2] as HTMLElement;
  field.innerText = '眼镜正面特写';
  fireEvent.blur(field);

  await user.click(screen.getByRole('button', { name: '全屏' }));
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByRole('article')).toBeInTheDocument();
  await user.click(within(dialog).getByRole('button', { name: '卡片视图' }));
  await user.click(screen.getByRole('menuitemradio', { name: '表格视图' }));
  const cells = within(dialog).getByRole('table').querySelectorAll('[contenteditable="true"]');
  expect((cells[2] as HTMLElement).innerText).toBe('眼镜正面特写');
  expect((cells[0] as HTMLElement).innerText).toBe('1');
  expect((cells[1] as HTMLElement).innerText).toBe('3');

  await user.click(within(dialog).getByRole('button', { name: '关闭' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('table')).toBeInTheDocument();
});

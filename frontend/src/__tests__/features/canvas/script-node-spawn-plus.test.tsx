// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';

import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { NodeSpawnPlusOverlay } from '@/features/canvas/ui/NodeSpawnPlusOverlay';
import { useCanvasStore } from '@/stores/canvasStore';

vi.mock('@xyflow/react', async () => {
  const actual = await vi.importActual<typeof import('@xyflow/react')>('@xyflow/react');
  return {
    ...actual,
    NodeToolbar: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    useStore: () => 1,
  };
});

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

beforeEach(() => {
  useCanvasStore.setState({
    nodes: [{
      id: 'script',
      type: CANVAS_NODE_TYPES.script,
      position: { x: 0, y: 0 },
      data: {
        scriptResult: {
          title: '眼镜展示',
          rows: [{ shot_no: 1, shot_prompt: '黑框眼镜棚拍' }],
        },
      },
    }],
    edges: [],
    activeOverlayNodeId: null,
  });
});

it('lets a hovered script open the downstream creation menu', () => {
  const onOpen = vi.fn();
  render(<NodeSpawnPlusOverlay hoveredNodeId="script" onPlusOpenMenu={onOpen} />);

  fireEvent.click(screen.getByRole('button', {
    name: 'canvas.spawnPlus.referenceThisNode',
  }));

  expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({
    nodeId: 'script',
    handleType: 'source',
  }));
  expect(screen.queryByRole('button', {
    name: 'canvas.spawnPlus.connectIntoThisNode',
  })).not.toBeInTheDocument();
});

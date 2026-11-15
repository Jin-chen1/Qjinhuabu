// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { create } from 'zustand';

/**
 * 画布指针工具。
 * - `move`：左键框选 / 拖节点，中键或空格拖动平移画布。
 * - `hand`：默认抓手，左键拖空白处平移画布；节点仍可选中、拖动、编辑和连线。
 */
export type CanvasTool = 'move' | 'hand';

interface CanvasToolState {
  tool: CanvasTool;
  setTool: (tool: CanvasTool) => void;
}

/**
 * 刻意不持久化：打开或刷新页面默认使用抓手，用户仍可随时切换移动工具。
 *
 * 单独一个轻量 store（和对齐吸附 / 触控板平移开关同构），订阅它的工具条不会因画布
 * 节点变动而重渲染。
 */
export const useCanvasToolStore = create<CanvasToolState>((set) => ({
  tool: 'hand',
  setTool: (tool) => set({ tool }),
}));

export const pages = ['overview', 'inputs', 'run', 'results', 'review', 'delivery', 'staffing'];

// 第三段在多数页面是明细偏移量，在排班推算页用作运行编号。
export function route() {
  const [page, id, offset] = location.hash.slice(1).split('/');
  return {page:pages.includes(page) ? page : 'overview', id:Number(id) || null, offset:Math.max(0, Number(offset) || 0)};
}

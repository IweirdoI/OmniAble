import { addFile, addProject, saveNote } from './project-library.js'

export const orderPracticeId = 'order-verification-sample-v1'

export function createOrderPractice(projects) {
  const project = addProject(projects, '订单核验练习（虚构样例）', orderPracticeId)
  const source = addFile(project, {
    name: '客户确认单.md', folder: '练习资料',
    content: '# 虚构练习 · 客户确认单\n\n订单：DEMO-01\n商品：蓝色收纳盒\n数量：3 件\n单价：28 元\n确认金额：84 元\n',
  })
  project.files.push(source)
  project.files.push(addFile(project, {
    name: '待核验订单.md', folder: '练习资料',
    content: '# 虚构练习 · 待核验订单\n\n订单：DEMO-01\n商品：蓝色收纳盒\n数量：2 件\n单价：28 元\n金额：56 元\n\n打开客户确认单，核对商品、数量、单价与金额，说明发现的差异和修改依据。',
  }))
  project.notes.push(saveNote(project, {
    title: '订单核验步骤', body: '先核对订单号，再逐项核对商品、数量、单价和金额。说明差异时指出客户确认单中的对应字段。',
    sourceId: source.id, sourceVersion: 1,
  }))
  return project
}

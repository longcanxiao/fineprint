import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'

export default function Chart({ option, height, group }: { option: echarts.EChartsOption; height: number; group?: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const inst = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    const el = ref.current!
    const c = echarts.init(el)
    inst.current = c
    if (group) {
      c.group = group
      echarts.connect(group)
    }
    const ro = new ResizeObserver(() => c.resize())
    ro.observe(el)
    return () => { ro.disconnect(); c.dispose() }
  }, [group])

  useEffect(() => { inst.current?.setOption(option, true) }, [option])

  return <div ref={ref} style={{ height, width: '100%' }} />
}

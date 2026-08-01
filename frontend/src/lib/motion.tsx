import React from 'react'

type MotionProps = Record<string, unknown> & {
  children?: React.ReactNode
}

type MotionTag = 'div' | 'tr'

function makeMotionElement(tag: MotionTag) {
  return React.forwardRef<HTMLElement, MotionProps>(function MotionElement(props, ref) {
    const { initial, animate, transition, whileHover, whileTap, ...rest } = props
    return React.createElement(tag, { ...rest, ref })
  })
}

export const motion = {
  div: makeMotionElement('div'),
  tr: makeMotionElement('tr'),
}

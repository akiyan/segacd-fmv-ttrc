OUTPUT_ARCH(m68k)

SECTIONS
{
  .text 0x006000 :
  {
    *(.text)
  }

  .data :
  {
    . = ALIGN(0x10);
    *(.data)
  }

  .bss :
  {
    *(.bss)
  }
}


OUTPUT_ARCH(m68k)

SECTIONS
{
  .text 0xFF8000 :
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

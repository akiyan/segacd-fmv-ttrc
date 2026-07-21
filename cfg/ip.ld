OUTPUT_ARCH(m68k)

SECTIONS
{
  .text 0xFF0000 :
  {
    *(.text)
  }

  .data :
  {
    . = ALIGN(0x10);
    *(.data)
  }

  ASSERT(. <= 0xFF2000, "Main IP permanent text/data overlaps transient codegen area")

  /* Keep the former startup-asset region as an explicit boundary for the
     generated Main-CPU code area. The minimal preload counter now reuses the
     permanent hexadecimal HUD font, so this section is normally empty. */
  .startup 0xFF2000 :
  {
    *(.startup)
    . = ALIGN(0x10);
  }

  ASSERT(. <= 0xFF4900, "startup section exceeds generated-code area")

  /* Runtime state occupies the free high-RAM area after the resident palette
     table. This keeps the permanent image out of the generated
     handler/blitter range. */
  .bss 0xFFD000 (NOLOAD) :
  {
    *(.bss)
  }

  ASSERT(. <= 0xFFFB00, "Main IP runtime state overlaps the stack guard")
}

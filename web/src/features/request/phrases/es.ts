import type { RequestPhrases } from './types';

export const es: RequestPhrases = {
  folderLabel: 'Edita los videos en',
  folderPlaceholder: '[indica la carpeta de los videos]',
  goalLabel: 'Objetivo',
  defaultGoal: 'convertir el material en un video claro y atractivo',
  formatLabel: 'Formato',
  paceLabel: 'Ritmo',
  captionsLabel: 'Subtítulos',
  visualsLabel: 'Visuales',
  sceneReference: reference => `Referencia del catálogo: ${reference}. Adapta el texto al material real.`,
  cleanupLabel: 'Limpieza estándar de la edición',
  cleanup: 'Elimina pausas innecesarias, respiraciones audibles, errores, falsos comienzos y repeticiones redundantes. Conserva las palabras completas, el ritmo natural, las pausas intencionales y el sentido del discurso. Mide los cortes sobre la grabación real y revisa las transiciones para que el habla no quede cortada. En revisiones puntuales, aplica la limpieza solo dentro del alcance de edición pedido; conserva el contenido no relacionado y los tiempos ya aprobados.',
  closing: 'Antes de editar, inspecciona localmente solo los archivos seleccionados, presenta un resumen completo de las opciones solicitadas y propuestas y espera mi confirmación, incluso cuando este prompt sea detallado. Incluye imagen y filtros, tratamiento de audio y música, subtítulos, idiomas de conversación/voz/subtítulos, cortes y pausas protegidas, formato/resolución/FPS/encuadre, elementos visuales, revisión de muestra y entrega. No agregues música, efectos de sonido ni ambiente de fondo salvo que lo pida explícitamente. Las escenas visuales, las transiciones y el ritmo dinámico no autorizan audio adicional. Conserva el audio que ya está en la grabación dentro del alcance solicitado. Conserva la apariencia original: no cambies brillo, tonalidad, contraste, saturación, filtros ni conviertas HDR/SDR sin solicitud. Explica las conversiones necesarias antes de continuar. Mantén el idioma grabado salvo que pida traducción. Guarda cada proyecto organizado en Downloads/editonny con exportaciones numeradas; si la ejecución es remota o no hay acceso, explica y confirma un destino accesible. Primero revisa el entorno y los archivos. Conserva los originales, las palabras habladas y el sentido. No inventes precios, resultados ni testimonios. Prepara una muestra de 8–15 segundos para que la revise antes de terminar el video completo. No uses servicios de pago ni subas contenido a terceros sin mi autorización. Al terminar, indica el archivo final y las comprobaciones realizadas.',
  formats: { source: 'conservar proporción, resolución y FPS originales', vertical: '9:16 (vertical)', horizontal: '16:9 (horizontal)', square: '1:1 (cuadrado)', portrait: '4:5 (retrato para el feed)' },
  paces: { natural: 'natural, conservando las pausas intencionales', dynamic: 'dinámico, con cortes precisos que conservan las palabras completas', calm: 'tranquilo, con espacio para la expresión y la comprensión' },
  captions: { readable: 'legibles, con un énfasis sutil en las palabras importantes', subtle: 'discretos, sin animación', none: 'sin subtítulos' },
  effects: { subtle: 'sutiles, solo cuando ayudan a explicar el discurso', educational: 'didácticos, con pasos, indicaciones y títulos', expressive: 'expresivos, con transiciones y énfasis equilibrados', none: 'sin escenas visuales ni efectos adicionales' },
};

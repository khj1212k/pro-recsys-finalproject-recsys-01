import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
  return twMerge(clsx(inputs));
}

export function mapCategoryIdToKey(id: number): any { // Return type 'Category' but easy to use 'any' to avoid circular dependency if types not imported
  const map: Record<number, string> = {
    100: 'politics',
    200: 'economy',
    300: 'it',
    400: 'society',
    500: 'culture',
    600: 'science',
    700: 'world'
  };
  return map[id] || 'society'; // Default fallback
}

export const formatDate = (date: Date): string => {
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  return `${yyyy}.${mm}.${dd}`;
};

export function mapCategoryKeyToCode(key: string): number {
    const map: Record<string, number> = {
        'politics': 100,
        'economy': 200,
        'it': 300,
        'society': 400,
        'culture': 500,
        'science': 600,
        'world': 700
    };
    return map[key] || 400;
}

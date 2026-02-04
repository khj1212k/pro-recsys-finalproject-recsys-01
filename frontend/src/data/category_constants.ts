import { Category } from '@/types';

export const categoryNames: Record<Category, string> = {
    politics: '정치',
    economy: '경제',
    it: 'IT/과학',
    society: '사회',
    culture: '생활/문화',
    sports: '스포츠',
    world: '세계',
};

// 7개 카테고리 컬러 설정
export const categoryColors: Record<Category, string> = {
    politics: 'bg-gray-500 text-white',
    economy: 'bg-yellow-500 text-white',
    it: 'bg-blue-500 text-white',
    society: 'bg-green-500 text-white',
    culture: 'bg-purple-400 text-white',
    sports: 'bg-indigo-500 text-white',
    world: 'bg-orange-400 text-white',
};

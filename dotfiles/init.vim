" https://github.com/Shougo/dein.vim
" https://github.com/Shougo/deoplete.nvim
" https://github.com/deoplete-plugins/deoplete-jedi
" https://github.com/deoplete-plugins/deoplete-go
" https://github.com/mdempsky/gocode

" Installation:
" - curl https://raw.githubusercontent.com/Shougo/dein.vim/master/bin/installer.sh > installer.sh
" - sh ./installer.sh ~/.nvim/dein
" - copy this file to ~/.config/nvim/init.vim
" - start nvim

"dein Scripts-----------------------------
if &compatible
  set nocompatible               " Be iMproved
endif

" Required:
set runtimepath+=/home/linux/.nvim/dein/repos/github.com/Shougo/dein.vim

" Required:
if dein#load_state('/home/linux/.nvim/dein/')
  call dein#begin('/home/linux/.nvim/dein/')

  " Let dein manage dein
  " Required:
  call dein#add('/home/linux/.nvim/dein/repos/github.com/Shougo/dein.vim')

  " Add or remove your plugins here:
  call dein#add('cyb70289/Zenburn')
  call dein#add('vim-scripts/cscope.vim')
  call dein#add('Shougo/deoplete.nvim', {'on_i': 1})
  call dein#add('deoplete-plugins/deoplete-jedi', {'on_ft': 'python'})
  call dein#add('deoplete-plugins/deoplete-go', {'on_ft': 'go'})

  " Required:
  call dein#end()
  call dein#save_state()
endif

" Required:
filetype plugin indent on
syntax enable

" If you want to install not installed plugins on startup.
if dein#check_install()
  call dein#install()
endif

"End dein Scripts-------------------------

let g:deoplete#enable_at_startup = 1

""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""

set ruler
set whichwrap=b,s,<,>,[,]
set backspace=indent,eol,start
set shiftwidth=4 tabstop=4 expandtab
set wrap hlsearch noincsearch ignorecase smartcase

let g:zenburn_disable_Label_underline = 1
colorscheme zenburn

" auto completion
inoremap <expr> <Tab> pumvisible() ? "\<C-n>" : "\<Tab>"
inoremap <expr> <S-Tab> pumvisible() ? "\<C-p>" : "\<S-Tab>"
"inoremap qq <C-O>:pc<CR>
autocmd InsertLeave * if pumvisible() == 0 | pclose | endif
set laststatus=1
set completeopt-=preview

" save last position
autocmd BufReadPost * if line("'\"") > 1 && line("'\"") <= line("$") | exe "normal! g'\"" | endif

" key mappings
nnoremap <F2> :set shiftwidth=4 tabstop=4 expandtab<CR>
nnoremap <F3> :set shiftwidth=8 tabstop=8 noexpandtab<CR>
nnoremap <F4> :nohlsearch<CR>

" cscope key mappings
nnoremap <leader>fa :call cscope#findInteractive(expand('<cword>'))<CR>
nnoremap <leader>l :call ToggleLocationList()<CR>
" s: Find this C symbol
nnoremap  <leader>fs :call cscope#find('s', expand('<cword>'))<CR>
" g: Find this definition
nnoremap  <leader>fg :call cscope#find('g', expand('<cword>'))<CR>
" d: Find functions called by this function
nnoremap  <leader>fd :call cscope#find('d', expand('<cword>'))<CR>
" c: Find functions calling this function
nnoremap  <leader>fc :call cscope#find('c', expand('<cword>'))<CR>
" t: Find this text string
nnoremap  <leader>ft :call cscope#find('t', expand('<cword>'))<CR>
" e: Find this egrep pattern
nnoremap  <leader>fe :call cscope#find('e', expand('<cword>'))<CR>
" f: Find this file
nnoremap  <leader>ff :call cscope#find('f', expand('<cword>'))<CR>
" i: Find files #including this file
nnoremap  <leader>fi :call cscope#find('i', expand('<cword>'))<CR>

" per directory configuration
autocmd BufNewFile,BufRead /home/linux/work/spdk/* set tabstop=8 shiftwidth=8 noexpandtab

" don't auto insert comment mark
autocmd BufNewFile,BufRead * setlocal formatoptions-=ro
